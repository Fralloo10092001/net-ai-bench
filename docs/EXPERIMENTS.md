# EXPERIMENTS — Campagne di valutazione su NetConfBench

Questo file descrive **cosa** misuriamo e **come** strutturiamo le campagne. Per le modifiche al codice che abilitano queste campagne vedi `TODO.md`. Per il testo narrativo del paper vedi `paper.tex`.

---

## Terminologia

Nel progetto convivono più concetti che rischiano di confondersi. Fissiamo i termini:

| Termine | Significato |
|---|---|
| **lab** | Topologia Containerlab (file YAML in `labs/`). È l'ambiente fisico emulato. |
| **task** | Una singola unità di valutazione (file JSON in `tasks/`): intent + lab + ground truth + testcases + baseline invariants + dominio. |
| **agent_mode** | Architettura dell'agente: `single_turn`, `react`, `planner_executor`. |
| **transactional** | Flag booleano ortogonale ad `agent_mode`: quando attivo, il server MCP tiene uno stack di snapshot per device (push automatico prima di ogni `apply_commands` / `write_config_file`) e l'agente ha accesso al tool `rollback(device)` che disfà l'ultima scrittura. |
| **cell** | Una tupla `(llm, agent_mode, transactional, task)`. È la più piccola unità valutabile. |
| **run** | L'esecuzione concreta di una cell. Una cell può avere N run (ripetizioni). |
| **experiment** | Una campagna che fissa un sottoinsieme di assi e varia gli altri. Ha obiettivo scientifico, setup, metriche attese, costo stimato. |

Scegliamo **experiment** come unità di campagna (non "laboratorio": in questo progetto "lab" è già la topologia Containerlab, userebbe lo stesso termine per due cose diverse).

---

## Metriche

Tutte calcolate per singolo run, poi aggregate.

| Simbolo | Definizione | Fonte |
|---|---|---|
| `S_testcase` | Frazione di testcase passati (output del comando = expected) | Già implementato |
| `S_command` | Similarità (edit distance o set-overlap) tra comandi dell'agente e `ground_truth_configs` | Già implementato |
| `S_reasoning` | Valutazione LLM-as-judge del reasoning finale contro `ground_truth_reasoning` | Già implementato |
| **`S_safety`** | Frazione di `baseline_invariants` ancora verdi dopo il run | **Nuovo** |
| `composite` | `S_testcase × S_safety` (se safety disponibile) | **Nuovo** |
| `outcome_class` | `clean_success` / `clean_fail` / `dangerous_success` / `destructive_fail` | **Nuovo** |
| `steps_used` | Numero di tool call fatte dall'agente | Già implementato |
| `cost_tokens` | Token consumati (in + out) | Già implementato |
| `rollback_used` | Bool: l'agente ha chiamato `rollback` almeno una volta nel run | **Nuovo** (solo transactional) |
| `rollback_count` | Int: quante volte ha chiamato `rollback` | **Nuovo** (solo transactional) |
| `max_undo_depth` | Int: profondità massima dello stack di snapshot raggiunta (≈ numero di scritture fatte) | **Nuovo** (solo transactional) |
| `failure_mode` | Tag manuale: `syntax` / `semantic` / `topology_misread` / `planning` / `validation` / `exhaustion` / `destructive` | **Nuovo**, post-hoc |

**Classificazione outcome** (dato un run):
```
                 S_safety == 1.0    S_safety < 1.0
S_testcase == 1  clean_success      dangerous_success
S_testcase < 1   clean_fail         destructive_fail
```

`dangerous_success` è la categoria più interessante scientificamente: l'agente ha "risolto il problema" ma ha rotto un invariant preesistente. È il failure mode che oggi nessuno misura.

---

## Pool di LLM e task

### LLM pool (da finalizzare in base a budget)

Minimo 3 frontier closed + 1 open-weight, tutti via LangChain:
- `claude-opus-4-7` (Anthropic)
- `claude-sonnet-4-6` (Anthropic)
- `gpt-5` o equivalente (OpenAI)
- `gemini-2.5-pro` (Google)
- Un open-weight via OpenRouter (es. `meta-llama/llama-3.3-70b-instruct` o `qwen-2.5-coder-72b`)

Temperature fissata a `0` per tutti, `max_steps = 35` per agent_mode con loop.

### Task suite (12 task, 6 domini)

| Dominio | Task |
|---|---|
| routing | `hello_world_ping`, `r1_static_routing`, `r2_frr_isis_ipv6` |
| firewall | `f1_anti_spoofing`, `f2_port_blocking` |
| nat | `n1_dnat_floating`, `n2_haproxy_lb` |
| tunneling | `t1_srv6_tunnel` |
| isolation | `v1_vrf_isolation` |
| troubleshooting | `ts1_isis_misconfig`, `ts2_ssh_filtered`, `ts3_srv6_broken` |

`frr_basic` e `srl02` restano come task "legacy" per regressione ma non entrano nelle tabelle principali.

---

## E1 — Cross-LLM baseline

**Obiettivo.** Ranking dei LLM a parità di scaffolding. Risponde: *quale LLM è il migliore per l'amministrazione di reti Linux?*

**Setup.**
- agent_mode: **fissato** a `react` (è il default del repo e il più comune in letteratura)
- LLM: tutto il pool
- task: tutti i 12 task
- ripetizioni: 1

**Celle totali.** `5 × 1 × 12 = 60 run`.

**Metriche riportate.** `S_testcase`, `S_safety`, `composite`, `outcome_class`, `steps_used`, `cost_tokens`.

**Output atteso.** Tabella `LLM × (metric aggregate su tutti i task)` + heatmap `LLM × domain` per success rate.

**Costo stimato.** ~60 run × ~40k token = 2.4M token totali. Con mix Opus/Sonnet/GPT-5/Gemini siamo nell'ordine dei $30–$60.

---

## E2 — Agent mode ablation

**Obiettivo.** Isolare il contributo dell'architettura agentica. Risponde: *a parità di LLM, passare da single-turn a ReAct a planner+executor quanto migliora (o peggiora) la performance?*

**Setup.**
- LLM: **fissato** al migliore di E1 (ipotesi: Opus 4.7 o GPT-5)
- agent_mode: tutti e tre
- task: tutti i 12 task
- ripetizioni: 1

**Celle totali.** `1 × 3 × 12 = 36 run`.

**Ipotesi da testare.**
1. Su task semplici (routing di base), `single_turn` è competitivo con `react` e molto più economico.
2. Su task di troubleshooting, `react` domina perché serve il feedback del validatore.
3. `planner_executor` può introdurre errori di planning che `react` non ha.
4. `planner_executor` ha `S_safety` migliore di `react` perché il piano esplicito riduce azioni "esplorative".

**Output atteso.** Bar chart `agent_mode × domain`, e tabella `steps_used` per mode (executor dovrebbe essere più economico di ReAct puro).

**Costo stimato.** ~36 run × ~30k token medi = 1.1M token, ~$15.

---

## E3 — Full matrix sweep

**Obiettivo.** Misurazione completa. Risponde: *l'effetto del LLM interagisce con l'agent_mode?* (Es.: un modello debole beneficia più del planner+executor? Un modello forte è sufficiente da solo in single-turn?)

**Setup.**
- LLM: tutto il pool
- agent_mode: tutti e tre
- task: tutti i 12 task
- ripetizioni: 1

**Celle totali.** `5 × 3 × 12 = 180 run`.

**Output atteso.**
- Tabella pivot completa `(LLM × agent_mode)` con `composite` medio
- Distribuzione `outcome_class` per `(LLM, agent_mode)` — dove si nasconde il `dangerous_success`?
- Istogramma `failure_mode` per dominio, una volta fatto il tagging manuale

**È la tabella principale del paper.**

**Costo stimato.** ~180 run × ~35k token = 6.3M token, ~$80–$120.

---

## E4 — Stability sub-study

**Obiettivo.** Dare significato statistico ai numeri. Risponde: *quanto rumore c'è run-to-run con temperature=0?* (Spoiler: non zero, per via di non-determinismo dei provider.)

**Setup.**
- LLM: 2 modelli (1 frontier + 1 open-weight)
- agent_mode: `react`
- transactional: `false`
- task: 3 task (1 facile, 1 medio, 1 hard)
- ripetizioni: **5**

**Celle totali.** `2 × 1 × 3 × 5 = 30 run`.

**Output atteso.** Tabella con media e std di `S_testcase` e `S_safety` per cella. Serve a giustificare (o meno) la scelta `repetitions=1` in E1–E3.

**Costo stimato.** Trascurabile.

---

## E5 — Transactional ablation

**Obiettivo.** Isolare l'effetto del wrapper transazionale (rollback agent-driven) sulla safety. Risponde: *l'accesso a `rollback` riduce i `dangerous_success` e `destructive_fail`? Gli LLM sono abbastanza "self-aware" da invocarlo quando serve, o il tool resta inutilizzato?*

**Perché non inglobare in E3.** Mettere transactional come 4° asse in E3 raddoppia le celle (da 180 a 360) senza che tutte le combinazioni siano interessanti. In particolare `single_turn × transactional` è degenere (nessun feedback loop in cui il rollback possa essere usato). Meglio una campagna focalizzata.

**Setup.**
- LLM: **fissato** al migliore di E1
- agent_mode: tutti e tre (mantenuti anche `single_turn` per completezza/controllo)
- transactional: **`false` e `true`**
- task: tutti i 12 task (ma l'analisi principale si concentra sui task con `baseline_invariants` non triviali, dove il rollback ha senso)
- ripetizioni: 1

**Celle totali.** `1 × 3 × 2 × 12 = 72 run`.

**Ipotesi da testare.**
1. Con `transactional=true`, i `destructive_fail` diminuiscono (l'LLM usa il rollback per recuperare).
2. Con `transactional=true`, `rollback_used` è significativamente più alto nei task dove l'approccio naive rompe un invariant — es. `f2_port_blocking` se l'agente applica una regola `DROP` troppo larga.
3. Il rollback è usato più in `react` che in `planner_executor` (il planner ragiona più a monte, fa meno esplorazione distruttiva).
4. Esiste un LLM che ha accesso al rollback ma non lo usa mai (evidenza di scarsa self-awareness).

**Output atteso.**
- Tabella `agent_mode × transactional` con distribuzione outcome_class.
- Per ogni cella transactional: `rollback_used` rate e media di `rollback_count`.
- Lista qualitativa dei run in cui il rollback ha "salvato" l'esito (transizione `destructive_fail → clean_success` rispetto alla controparte non-transactional sullo stesso task).

**Costo stimato.** ~72 run × ~40k token medi (più token perché prompt più lungo per il rollback tool) = 2.9M token, ~$40.

---

## Analisi derivate (su dati E3 + E5)

Non sono esperimenti separati — riutilizzano i run di E3, applicando analisi diverse.

### A1 — Analisi per dominio
Tabella pivot `domain × (LLM, agent_mode)`. Evidenzia i domini dove tutti falliscono (candidate per il dataset "hard" futuro) e quelli saturi (candidate da rimuovere).

### A2 — Failure taxonomy
Tagging manuale via `analysis/failure_tagging.py` su un campione di run falliti (stratificato per dominio). Produce la distribuzione dei `failure_mode`. **Questo è il contributo qualitativo più forte del paper.**

### A3 — Safety drill-down
Focus sui `dangerous_success` e `destructive_fail`: quali invariants rompono più spesso? Esiste un pattern per agent_mode? Per dominio? Sui run di E5, incrocio con `rollback_used` per isolare i casi di recupero riuscito.

### A4 — Efficienza
Scatter `composite_score` vs `cost_tokens` per cella. Frontier di Pareto: quale combinazione `(LLM, agent_mode)` massimizza la qualità per dollaro?

---

## Ordine di esecuzione consigliato

1. **E4** prima di tutto — sanity check sulla varianza, ~1 ora.
2. **E1** — ranking LLM, ~mezza giornata.
3. **E2** — scelta dell'LLM pilota basata su E1.
4. **E3** — la campagna grossa, in overnight.
5. **E5** — transactional ablation, dopo aver scelto l'LLM pilota (può girare in parallelo con l'analisi di E3).
6. **Analisi A1–A4** — iterativa, dopo E3 + E5.

Tra E1–E3 c'è overlap (E1 ⊂ E3). Con `--skip-existing` (vedi TODO punto 6) si evita di rifare celle. E5 è disgiunto perché aggiunge l'asse `transactional`.

---

## Riproducibilità

Ogni run salva in `results/runs/<task>/<provider>__<model>/<agent_mode>[__tx]/rep<N>/` (suffisso `__tx` se `transactional=true`):
- `run.json` (RunResult serializzato)
- `history.jsonl` (tool call + risposte)
- `evaluation.json` (tutte le metriche)
- `config_snapshot.json` (running config dei device prima e dopo)

L'aggregato è `results.csv`, una riga per run, colonne definite in TODO.md punto 6.

Seed LLM non garantito (i provider non espongono seed stabili), ma temperature=0 e prompt deterministici danno la miglior riproducibilità ottenibile oggi.
