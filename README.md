# NetConfBench – Benchmark per agenti LLM su configurazioni di rete

NetConfBench è un framework leggero per valutare agenti basati su Large Language Models (LLM) su task di configurazione di rete, utilizzando ambienti containerizzati con Containerlab.

L’obiettivo del progetto è fornire:
- un sistema semplice per testare agenti su scenari di rete realistici
- un modo strutturato per valutare le prestazioni (correttezza, comandi, reasoning)
- una pipeline facilmente estendibile con nuovi scenari e modelli

---

## Struttura del progetto

netconfbench/
├── agent/
├── ani/
├── benchmark/
├── labs/
├── tasks/
├── logs/
├── results/
└── requirements.txt

---

## Requisiti

- Python 3.10+
- Docker
- Containerlab
- API key per provider LLM

Installazione:

pip install -r requirements.txt

---

## Uso

Eseguire un agente:

python -m agent.agent_runner --lab frr-basic --task tasks/frr_basic.json --provider google --model gemini-2.5-flash

Eseguire benchmark:

python benchmark/run_benchmark.py

---

## logs vs results

logs/: ultimo run (temporaneo)  
results/: output persistente e confrontabile  

---

## Conclusione

NetConfBench permette di testare e confrontare agenti LLM su scenari di rete in modo semplice e riproducibile.
