import random
import time
from collections import deque
from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST

app = FastAPI(title="Error Budget POC")


app.mount("/static", StaticFiles(directory="static"), name="static")

# ──────────────────────────────────────────
# Métricas Prometheus
# ──────────────────────────────────────────
REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total de requisições HTTP",
    ["method", "endpoint", "status_code"],
)

REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "Latência das requisições HTTP",
    ["endpoint"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

ERROR_BUDGET_CONSUMED = Gauge(
    "error_budget_consumed_ratio",
    "Fração do error budget consumido (0 a 1)",
)

SLO_TARGET = Gauge(
    "slo_target_ratio",
    "SLO alvo definido (ex: 0.99 = 99%)",
)

BURN_RATE_1H = Gauge(
    "error_budget_burn_rate_1h",
    "Burn rate em 1h (quantos % do budget é queimado por hora)",
)

BURN_RATE_5M = Gauge(
    "error_budget_burn_rate_5m",
    "Burn rate em 5m (quantos % do budget é queimado por 5 minutos)",
)

# Estado interno de contagem
_total_requests = 0
_error_requests = 0
_force_error_rate = 0.0   # 0.0 = sem forçar erros extras
_slo_target = 0.99        # SLO: 99% de disponibilidade (variável)

# Histórico de requisições (timestamp, é_erro) para cálculo de burn rate
_request_history = deque(maxlen=10000)  # Manter últimas 10k requisições

SLO_TARGET.set(_slo_target)

# ──────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────
def _calculate_burn_rate(window_seconds: int):
    """
    Calcula o burn rate normalizado para 30 dias (mês).
    
    Burn Rate = (error_rate_na_janela / allowed_error_rate) × (total_minutos_mês / minutos_da_janela)
    
    Exemplo: Burn Rate 1h = quanto do orçamento mensal queima em 1 hora
    """
    if not _request_history:
        return 0
    
    now = time.time()
    window_start = now - window_seconds
    
    # Filtrar requisições na janela
    requests_in_window = [
        (ts, is_error) for ts, is_error in _request_history
        if ts >= window_start
    ]
    
    if not requests_in_window:
        return 0
    
    total_in_window = len(requests_in_window)
    errors_in_window = sum(1 for _, is_error in requests_in_window if is_error)
    error_rate = errors_in_window / total_in_window
    allowed_error_rate = 1 - _slo_target
    
    if allowed_error_rate <= 0:
        return 0
    
    # Burn rate bruto (quantas vezes o allowed_error_rate foi excedido)
    burn_rate_raw = error_rate / allowed_error_rate
    
    # Normalizar para 30 dias (43200 minutos)
    window_minutes = window_seconds / 60
    month_minutes = 30 * 24 * 60  # 43200 minutos
    burn_rate_normalized = burn_rate_raw * (month_minutes / window_minutes)
    
    return burn_rate_normalized


def _update_burn_rate_metrics():
    """Atualiza métricas de burn rate."""
    burn_rate_1h = _calculate_burn_rate(3600)
    burn_rate_5m = _calculate_burn_rate(300)
    BURN_RATE_1H.set(burn_rate_1h)
    BURN_RATE_5M.set(burn_rate_5m)


def _update_error_budget():
    """Recalcula e expõe o consumo do error budget."""
    global _total_requests, _error_requests, _slo_target
    if _total_requests == 0:
        ERROR_BUDGET_CONSUMED.set(0)
        return
    error_rate = _error_requests / _total_requests
    allowed_error_rate = 1 - _slo_target
    consumed = min(error_rate / allowed_error_rate, 1.0) if allowed_error_rate > 0 else 0
    ERROR_BUDGET_CONSUMED.set(consumed)


def _register(endpoint: str, status: int, duration: float):
    global _total_requests, _error_requests
    _total_requests += 1
    is_error = status >= 500
    if is_error:
        _error_requests += 1
    
    # Adicionar ao histórico para burn rate
    _request_history.append((time.time(), is_error))
    
    REQUEST_COUNT.labels(method="GET", endpoint=endpoint, status_code=str(status)).inc()
    REQUEST_LATENCY.labels(endpoint=endpoint).observe(duration)
    _update_error_budget()
    _update_burn_rate_metrics()

# ──────────────────────────────────────────
# Endpoints da aplicação simulada
# ──────────────────────────────────────────
@app.get("/healthy")
def healthy():
    """Endpoint sempre saudável — nunca falha."""
    start = time.time()
    _register("/healthy", 200, time.time() - start)
    return {"status": "ok"}


@app.get("/unstable")
def unstable():
    """Endpoint instável — falha com base na taxa configurada."""
    start = time.time()
    time.sleep(random.uniform(0.05, 0.3))
    if random.random() < _force_error_rate:
        _register("/unstable", 500, time.time() - start)
        return Response(content='{"error": "simulated failure"}', status_code=500, media_type="application/json")
    _register("/unstable", 200, time.time() - start)
    return {"status": "ok", "note": "unstable endpoint — may fail"}


@app.get("/slow")
def slow():
    """Endpoint lento — latência alta simulada."""
    start = time.time()
    time.sleep(random.uniform(1.0, 3.0))
    _register("/slow", 200, time.time() - start)
    return {"status": "ok", "note": "slow response"}


# ──────────────────────────────────────────
# Controle de injeção de falhas
# ──────────────────────────────────────────
@app.post("/fault/set/{rate}")
def set_fault_rate(rate: float):
    """
    Define a taxa de erros forçados no /unstable.
    rate: 0.0 (sem erros) até 1.0 (100% de erros)
    """
    global _force_error_rate
    _force_error_rate = max(0.0, min(1.0, rate))
    return {"fault_rate_set": _force_error_rate}


@app.get("/fault/status")
def fault_status():
    """Retorna configuração atual de injeção de falhas e estado do SLO."""
    global _total_requests, _error_requests, _force_error_rate, _slo_target
    error_rate = (_error_requests / _total_requests) if _total_requests > 0 else 0
    allowed_error_rate = 1 - _slo_target
    consumed = min(error_rate / allowed_error_rate, 1.0) if allowed_error_rate > 0 else 0
    return {
        "fault_rate": _force_error_rate,
        "total_requests": _total_requests,
        "error_requests": _error_requests,
        "error_rate": round(error_rate, 4),
        "slo_target": _slo_target,
        "error_budget_consumed": round(consumed, 4),
        "error_budget_remaining": round(1.0 - consumed, 4),
    }


@app.post("/fault/reset")
def reset_counters():
    """Zera os contadores de requisições (reinicia a janela do SLO)."""
    global _total_requests, _error_requests, _force_error_rate, _request_history
    _total_requests = 0
    _error_requests = 0
    _force_error_rate = 0.0
    _request_history.clear()
    ERROR_BUDGET_CONSUMED.set(0)
    BURN_RATE_1H.set(0)
    BURN_RATE_5M.set(0)
    return {"reset": True}


# ──────────────────────────────────────────
# SLO Configuration
# ──────────────────────────────────────────
@app.post("/slo/set/{target}")
def set_slo(target: float):
    """
    Define o SLO alvo.
    target: 0.0 a 1.0 (ex: 0.99 = 99%)
    """
    global _slo_target
    _slo_target = max(0.0, min(1.0, target))
    SLO_TARGET.set(_slo_target)
    _update_error_budget()
    return {"slo_target_set": _slo_target}


# ──────────────────────────────────────────
# UI - Dashboard
# ──────────────────────────────────────────
@app.get("/")
def serve_ui():
    """Serve the dashboard UI."""
    return FileResponse("static/index.html", media_type="text/html")


# ──────────────────────────────────────────
# Métricas para o Prometheus
# ──────────────────────────────────────────
@app.get("/metrics")
def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
