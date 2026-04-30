import random
import time
from fastapi import FastAPI, Response
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST

app = FastAPI(title="Error Budget POC")

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

# Estado interno de contagem
_total_requests = 0
_error_requests = 0
_force_error_rate = 0.0   # 0.0 = sem forçar erros extras

SLO_TARGET.set(0.99)  # SLO: 99% de disponibilidade

# ──────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────
def _update_error_budget():
    """Recalcula e expõe o consumo do error budget."""
    global _total_requests, _error_requests
    if _total_requests == 0:
        ERROR_BUDGET_CONSUMED.set(0)
        return
    error_rate = _error_requests / _total_requests
    slo = 0.99
    allowed_error_rate = 1 - slo          # 1%
    consumed = min(error_rate / allowed_error_rate, 1.0)
    ERROR_BUDGET_CONSUMED.set(consumed)


def _register(endpoint: str, status: int, duration: float):
    global _total_requests, _error_requests
    _total_requests += 1
    if status >= 500:
        _error_requests += 1
    REQUEST_COUNT.labels(method="GET", endpoint=endpoint, status_code=str(status)).inc()
    REQUEST_LATENCY.labels(endpoint=endpoint).observe(duration)
    _update_error_budget()

# ──────────────────────────────────────────
# Endpoints da aplicação simulada
# ──────────────────────────────────────────
@app.get("/healthy")
def healthy():
    """Endpoint sempre saudável — nunca falha."""
    start = time.time()
    time.sleep(random.uniform(0.01, 0.05))
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
    global _total_requests, _error_requests, _force_error_rate
    error_rate = (_error_requests / _total_requests) if _total_requests > 0 else 0
    slo = 0.99
    allowed_error_rate = 1 - slo
    consumed = min(error_rate / allowed_error_rate, 1.0) if allowed_error_rate > 0 else 0
    return {
        "fault_rate": _force_error_rate,
        "total_requests": _total_requests,
        "error_requests": _error_requests,
        "error_rate": round(error_rate, 4),
        "slo_target": slo,
        "error_budget_consumed": round(consumed, 4),
        "error_budget_remaining": round(1.0 - consumed, 4),
    }


@app.post("/fault/reset")
def reset_counters():
    """Zera os contadores de requisições (reinicia a janela do SLO)."""
    global _total_requests, _error_requests, _force_error_rate
    _total_requests = 0
    _error_requests = 0
    _force_error_rate = 0.0
    ERROR_BUDGET_CONSUMED.set(0)
    return {"reset": True}


# ──────────────────────────────────────────
# Métricas para o Prometheus
# ──────────────────────────────────────────
@app.get("/metrics")
def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
