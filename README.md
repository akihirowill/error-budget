# Error Budget POC — SRE / SLO-based

Stack de observabilidade para demonstrar consumo de **error budget** com base em SLO.

## Stack

| Componente | Tecnologia | Porta |
|---|---|---|
| Aplicação | Python + FastAPI | 8000 |
| Métricas | Prometheus | 9090 |
| Dashboards | Grafana | 3000 |

---

## Pré-requisitos (Oracle Cloud VM)

```bash
# Docker + Docker Compose
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git
sudo usermod -aG docker $USER
newgrp docker
```

---

## Deploy local / Oracle Cloud

```bash
git clone https://github.com/SEU_USER/error-budget-poc.git
cd error-budget-poc

docker compose up -d --build

# Verificar
docker compose ps
```

Acesse:
- **App:** http://<IP>:8000/docs  (Swagger automático)
- **Prometheus:** http://<IP>:9090
- **Grafana:** http://<IP>:3000  (admin / admin123)

> **Oracle Cloud:** abra as portas 8000, 9090 e 3000 no Security List da VCN.

---

## Endpoints da aplicação

| Método | Endpoint | Descrição |
|---|---|---|
| GET | `/healthy` | Sempre retorna 200 |
| GET | `/unstable` | Falha conforme a taxa configurada |
| GET | `/slow` | Resposta lenta (1–3s) |
| GET | `/metrics` | Métricas Prometheus |
| POST | `/fault/set/{rate}` | Define taxa de erros (0.0 a 1.0) |
| GET | `/fault/status` | Estado atual do SLO e error budget |
| POST | `/fault/reset` | Zera contadores |

---

## Cenários de teste

### 1. Baseline — tudo saudável
```bash
# Gera tráfego no endpoint estável
for i in $(seq 1 100); do curl -s http://localhost:8000/healthy > /dev/null; done
```

### 2. Injetar falhas — 10% de erros
```bash
# Ativa 10% de falhas
curl -X POST http://localhost:8000/fault/set/0.1

# Gera tráfego
for i in $(seq 1 200); do curl -s http://localhost:8000/unstable > /dev/null; sleep 0.1; done
```

### 3. Queimar o error budget — 50% de erros
```bash
curl -X POST http://localhost:8000/fault/set/0.5
for i in $(seq 1 500); do curl -s http://localhost:8000/unstable > /dev/null; done
```

### 4. Verificar status do SLO
```bash
curl http://localhost:8000/fault/status | python3 -m json.tool
```

### 5. Resetar e recomeçar
```bash
curl -X POST http://localhost:8000/fault/reset
```

---

## Métricas expostas

| Métrica | Tipo | Descrição |
|---|---|---|
| `http_requests_total` | Counter | Total de requests por endpoint e status |
| `http_request_duration_seconds` | Histogram | Latência por endpoint |
| `error_budget_consumed_ratio` | Gauge | Fração do budget consumido (0–1) |
| `slo_target_ratio` | Gauge | SLO alvo (0.99) |
| `job:http_error_rate:ratio5m` | Recording rule | Taxa de erro (janela 5m) |
| `job:error_budget_burn_rate:1h` | Recording rule | Burn rate em 1h |
| `job:error_budget_burn_rate:5m` | Recording rule | Burn rate em 5m |

---

## CI/CD — GitHub Actions

O pipeline (`.github/workflows/ci-cd.yml`) executa em todo push para `main`:

1. **Build** — constrói a imagem Docker
2. **Smoke test** — sobe o container e valida `/healthy` e `/metrics`
3. **Deploy** — conecta via SSH na VM Oracle Cloud e executa `docker compose up -d`

### Secrets necessários no GitHub

| Secret | Valor |
|---|---|
| `OCI_HOST` | IP público da VM |
| `OCI_USER` | Usuário SSH (ex: `ubuntu` ou `opc`) |
| `OCI_SSH_KEY` | Chave privada SSH (conteúdo do arquivo `.pem`) |

---

## SLO configurado

- **SLO:** 99% de disponibilidade  
- **Error budget:** 1% das requisições podem falhar  
- **Burn rate > 2x** → alerta warning  
- **Budget consumido > 80%** → alerta critical
