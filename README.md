# NetConfBench

**NetConfBench** è un framework per valutare agenti basati su Large Language Models (LLM) su task di configurazione di rete, utilizzando ambienti containerizzati con [Containerlab](https://containerlab.dev/).

L'agente riceve un obiettivo espresso in linguaggio naturale (es. *"fai comunicare PC1 con PC2 attraverso due router"*), ispeziona la topologia, legge la configurazione corrente dei dispositivi, applica i comandi necessari e valida il risultato — tutto in autonomia.

---

## Architettura

```
┌─────────────────────────────────────────────────────────────────┐
│                     Benchmark Orchestrator                      │
│          run_benchmark.py  ·  loop modelli × task               │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│                        Agent Runner                             │
│     agent_runner.py  ·  loop ReAct (max N step)                 │
│     system prompt  ·  history  ·  parse / retry                 │
└────────────┬─────────────────────────────────────┬──────────────┘
             │                                     │
┌────────────▼──────────────┐       ┌──────────────▼──────────────┐
│       LLM Provider        │       │       MCP Server (ANI)      │
│   model_registry.py       │       │       mcp_server.py         │
│   LangChain wrapper       │       │   get_topology              │
│   OpenAI / Anthropic /    │       │   get_running_config        │
│   Google / OpenRouter     │       │   update_config             │
└───────────────────────────┘       │   execute_validation        │
                                    └──────────┬──────────────────┘
                                               │
                               ┌───────────────┴───────────────┐
                               │                               │
                  ┌────────────▼────────────┐   ┌─────────────▼────────────┐
                  │      LinuxDriver        │   │      SRLinuxDriver       │
                  │  allowlist comandi ip   │   │  sr_cli scripting        │
                  └────────────┬────────────┘   └─────────────┬────────────┘
                               └───────────────┬───────────────┘
                                               │
                              ┌────────────────▼────────────────┐
                              │      Containerlab / Docker      │
                              │   nodi di rete virtualizzati    │
                              └─────────────────────────────────┘

             ┌─────────────────────────────────────────────┐
             │                  Evaluator                  │
             │  evaluator.py                               │
             │  S_testcase  (50%)  — ping / ip addr / ...  │
             │  S_command   (30%)  — F1 vs ground truth    │
             │  S_reasoning (20%)  — proxy comportamentale │
             │  ──────────────────────────────────────────  │
             │  summary_score = weighted average           │
             └─────────────────────────────────────────────┘
```

### Flusso di esecuzione

1. **`run_benchmark.py`** carica la lista di modelli (`benchmark/models.json`) e di task (`benchmark/tasks.json`), resetta il lab Containerlab per ogni task, avvia l'agente e infine salva i risultati.
2. **`agent_runner.py`** istanzia il modello LLM tramite `model_registry.py` e avvia un loop ReAct: a ogni passo costruisce un prompt (system prompt + storia degli ultimi 4 step) e chiede al modello di restituire un JSON con `thought`, `action`, `tool_name`, `tool_args`. Gestisce errori di parsing con feedback esplicito al modello.
3. **MCP Server** (`ani/mcp_server.py`) espone quattro strumenti via [FastMCP](https://gofastmcp.com/) su stdio. Legge la topologia dal file `.yml` del lab e seleziona il driver corretto in base al `kind` del nodo.
4. I **driver** (`LinuxDriver`, `SRLinuxDriver`) traducono le chiamate agli strumenti in comandi Docker exec, con validazione di sicurezza (allowlist, blocklist).
5. Al termine, **`evaluator.py`** misura tre dimensioni e calcola un punteggio aggregato.

---

## Struttura del progetto

```
net-ai-bench/
├── agent/
│   ├── agent_runner.py      # loop ReAct, prompt building, gestione storia
│   └── model_registry.py   # factory LLM via LangChain
├── ani/
│   ├── mcp_server.py        # MCP server (ANI — Agent-Network Interface)
│   └── drivers/
│       ├── base.py          # BaseDriver (ABC)
│       ├── linux_driver.py  # driver per nodi Linux
│       ├── srlinux_driver.py# driver per Nokia SR Linux
│       └── __init__.py      # factory get_driver()
├── benchmark/
│   ├── evaluator.py         # metriche S_testcase, S_command, S_reasoning
│   ├── run_benchmark.py     # orchestratore principale
│   ├── models.json          # lista modelli da testare
│   └── tasks.json           # lista task da eseguire
├── labs/                    # file .yml Containerlab
│   ├── hello-world.yml
│   ├── frr-basic.yml
│   ├── srl02.yml
│   └── ...
├── tasks/                   # definizioni dei task (JSON)
│   ├── hello_world_ping.json
│   ├── frr_basic.json
│   ├──srl02.json
│   └── ...
├── results/                 # output persistente dei benchmark
│   ├── results.json
│   ├── results.csv
│   └── runs/                # artefatti per ogni run (modello × task)
├── logs/                    # output dell'ultimo run (temporaneo)
└── requirements.txt
```

---

## Requisiti

- Python 3.10+
- Containerlab
- API key per almeno un provider LLM

---

## Installazione

```bash
git clone https://github.com/netgroup/net-ai-bench
cd net-ai-bench
pip install -r requirements.txt
cp .env.example .env   # inserisci le API key
```

Variabili d'ambiente richieste in `.env` (solo quelle relative ai provider che si intende usare):

```
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
GOOGLE_API_KEY=...
OPENROUTER_API_KEY=...
```

---

## Uso

### Eseguire un singolo agente

```bash
python -m agent.agent_runner \
  --lab frr-basic \
  --task tasks/frr_basic.json \
  --provider google \
  --model gemini-2.5-flash
```

I log vengono scritti in `logs/` (sovrascrivibili a ogni run).

| Argomento | Valori accettati |
|-----------|-----------------|
| `--provider` | `openai`, `anthropic`, `google`, `openrouter` |
| `--model` | qualsiasi model string valida per il provider scelto |
| `--max-steps` | intero, default `25` |

### Eseguire il benchmark completo

```bash
python benchmark/run_benchmark.py
```

Esegue tutti i modelli definiti in `benchmark/models.json` su tutti i task in `benchmark/tasks.json`. I risultati vengono salvati in `results/results.json` e `results/results.csv`, con gli artefatti di ogni singolo run in `results/runs/<task>/<provider>__<model>/`.

### Eseguire solo il valutatore

```bash
python benchmark/evaluator.py \
  --task tasks/frr_basic.json \
  --lab frr-basic
```

---

## Aggiungere un nuovo task

Creare un file JSON in `tasks/` con la seguente struttura:

```json
{
  "task_name": "Nome del task",
  "lab_file": "labs/nome-lab.yml",
  "intent": "Descrizione in linguaggio naturale dell'obiettivo",
  "ground_truth_configs": {
    "nome-dispositivo": [
      "comando1",
      "comando2"
    ]
  },
  "ground_truth_reasoning": "Spiegazione della soluzione corretta",
  "testcases": [
    {
      "name": "Descrizione del test",
      "device": "nome-dispositivo",
      "command": "comando di verifica",
      "expected_output": "stringa attesa nell'output"
    }
  ]
}
```

Aggiungere il path del task a `benchmark/tasks.json` per includerlo nel benchmark completo.

---

## Aggiungere un nuovo driver

1. Creare `ani/drivers/mio_driver.py` estendendo `BaseDriver`:

```python
from .base import BaseDriver

class MioDriver(BaseDriver):
    def get_running_config(self, device: str) -> dict: ...
    def update_config(self, device: str, commands: list[str]) -> dict: ...
    def execute_validation(self, device: str, command: str) -> dict: ...
```

2. Registrarlo in `ani/drivers/__init__.py`:

```python
from .mio_driver import MioDriver

def get_driver(kind: str, lab_name: str, log_file: Path):
    if kind == "mio_kind":
        return MioDriver(lab_name=lab_name, log_file=log_file)
    ...
```

Il `kind` corrisponde al campo `kind:` nei file `.yml` di Containerlab.

---

## Sistema di valutazione

Ogni run produce un `summary_score` calcolato come:

```
summary_score = 0.5 × S_testcase + 0.3 × S_command + 0.2 × S_reasoning
```

| Metrica | Descrizione |
|---------|-------------|
| **S_testcase** | Frazione di testcase superati (comandi di verifica eseguiti sui container) |
| **S_command** | F1-score tra i comandi applicati e i `ground_truth_configs` |
| **S_reasoning** | Proxy comportamentale: topologia ispezionata per prima, config letta presto, validazione eseguita, task completato, penalità per errori e step eccessivi |

---

## `logs/` vs `results/`

| Directory | Contenuto |
|-----------|-----------|
| `logs/` | Output dell'ultimo run (sovrascritto a ogni esecuzione) |
| `results/` | Output persistente e confrontabile tra modelli e run successivi |

---

## Aggiungere un modello

Aggiungere una voce a `benchmark/models.json`:

```json
[
  { "provider": "google",      "model": "gemini-2.5-flash" },
  { "provider": "anthropic",   "model": "claude-opus-4-5" },
  { "provider": "openai",      "model": "gpt-4o" },
  { "provider": "openrouter",  "model": "meta-llama/llama-3.3-70b-instruct" }
]
```