# Portfolio Capacity Research v1

## Frozen research question

Does removing the active three-position count bottleneck improve portfolio
outcomes while entry eligibility, strategy, exit, per-trade risk, total/symbol
exposure and correlation constraints remain unchanged?

This hypothesis arose from the **2024 EXPLORATION** slot diagnostic: 2699
baseline max-position rejections and post-hoc upside potential, without a proven
recognizable replacement edge. Incumbents were frequently profitable themselves.
There is no replacement policy in this experiment.

- Executable base: `75998cae198f09f7be33bf5777f048b9653d7f81`
- Base branch: `feature/institutional-entry-ranking-v1`
- Research branch: `research/portfolio-capacity-v1`
- Config: `docs/replay/portfolio-capacity-v1-2024-exploration.config.json`
- Name: `2024 EXPLORATION - Portfolio Capacity Research v1`
- Exactly two variants, A and C. No annual replay is authorized by this preparation.

## Frozen variants

| Setting | A — Institutional Baseline Control | C — Exposure-Limited Capacity |
|---|---|---|
| Structural state | COUNT_CONSTRAINED_3 | COUNT_EFFECTIVELY_UNCONSTRAINED, EXPOSURE_CONSTRAINED_50_PERCENT |
| max_open_positions | 3 | 100 |
| Strategy | institutional_style_v1 | identical |
| Exit | baseline_v1, empty parameters | identical |
| entry_ranking / entry_admission / setup_rearm | OFF / OFF / OFF | identical |
| entry_filter | neutral | identical |
| Outer BTC regime entry gate | OFF | identical |
| Institutional BTC regime / correlation logic | original baseline settings | identical |

All institutional strategy parameters are copied from the reviewed ranking A
baseline, including its multi-timeframe/BTC regime configuration and correlation
guard. No new eligibility, ranking, score, sizing, delta15 or relative-volume2
filter is introduced. Neutral/OFF defaults are normalized by the existing backend.

Shared risk: initial balance 1000 USDT; risk/trade 0.5%; total exposure 50%; symbol
exposure 20%; daily loss/max drawdown 100%/100%; minimum order 10 USDT; fee 0.1%;
slippage 0.05%; pyramiding false. **Only the count cap differs.**

Shared run: 2024-01-01T00:00:00Z inclusive to 2025-01-01T00:00:00Z exclusive,
EXPLORATION, historical Top20, minimum quote volume 5000000, spread approximation
0.02%, candidate_symbols null, fallback_5m false, conservative true,
RESEARCH_QUARANTINE_STALE. ISOLATED_TOPN_V1 is backend-derived and is deliberately
absent from public import JSON. All other shared fields/defaults match the base.

### Why 100 is a sentinel, not a tuned position count

We compare two structural states, not candidate settings 4/5/6/8/etc. No optimal
position count is sought. The finite schema-supported sentinel 100 is intended to
make count nonbinding under normal conditions; the unchanged RiskManager and 50%
exposure cap continue to size/admit positions. Correlation, symbol exposure,
minimum order and cash may remain binding. If count 100 actually binds, report it;
do not silently describe C as unlimited or choose a new sentinel.

Exposure is marked market value, not a price-independent clamp: price movements
can take an existing portfolio above its admission limit. Do not change risk or
marking semantics to force measured maximum exposure to 50%.

If C later looks promising, operational position count and clean validation are
separate research questions, outside this experiment. No parameter sweep or
threshold adjustment follows automatically.

## Exact one-field verification

Use the existing `validate` and `input_config` functions. Normalize via the backend
and project back onto its **existing public schema**. Exclude only `name` as a
presentation label for the A/C experimental-parameter comparison. Do not introduce
another config schema or custom defaults.

The complete remaining public variant dictionaries differ exactly at:

```json
[{"path": "risk_parameters.max_open_positions", "A": 3, "C": 100}]
```

For transparency, the **full internal backend snapshot** has these three leaf
paths differing, and no others:

1. `name`: the requested A/C presentation labels;
2. `risk_parameters.max_open_positions`: 3 versus 100;
3. `paper_settings.max_open_positions`: the same 3 versus 100, copied by the
   unchanged backend into its derived effective settings.

The third path is a checked materialization of the one risk parameter, not a
second independently varied control. A literal internal JSON diff including names
and derived settings is therefore not a one-leaf diff; the assertion below checks
both representations explicitly. No unexpected difference is ignored.

Normalized A equals the original ranking A variant in full, including derived
settings. All shared normalized fields, except run name/variant list, equal the
original ranking config. This proves **config parity**, not newly executed annual
financial parity; no replay or synthetic runtime test is started for this task.

## Methodology and future metrics frozen before replay

2024 is EXPLORATION because this hypothesis was formed using its diagnostic
results. It cannot now serve as clean validation. Do not run 2025, 2023 or touch
2026 for this task. A clean validation plan would require separate authorization.
No winner promotion, replacement rule or parameter optimization is implied.

A later authorized replay must preserve the config, exact executable commit and
source/catalog revisions in its exports. Check A's financial control parity
against ranking run `31423539f64e4650840df5b6b3fe9a26` under identical input data
before interpreting C: trades, timestamps, quantities, orders/fills, cash, equity,
PnL, fees and slippage. Companion isolation and no-lookahead remain required.

### Headline

Report A/C final equity, net PnL, return %, PF, expectancy R, average R, max DD,
closed/open/open_stale counts, win rate, turnover, fees, slippage, average/max
exposure. Keep realized trade metrics distinct from final equity including open
marks. Stale valuations and data gaps must remain visible.

### Capacity

- Maximum simultaneous open positions, time-weighted average open count, and
  duration-weighted position-count distribution over the entire run, including
  zero positions and terminal open/stale positions.
- Reconstruct state from actual audited entry/exit order and UTC timestamps.
  Preserve actual event sequence at equal timestamps. Integrate piecewise-constant
  count over elapsed simulated time; do not substitute an average of trade counts
  or uneven samples. Zero-duration intermediate states do not add duration.
- Report max-position, total-exposure, symbol-exposure, correlation, cash and
  minimum-order blocks using existing raw reasons and replay rejection categories.
- Preserve the combined existing reason
  `sized order below minimum value or insufficient free balance/exposure room`
  as a combined bucket; do not invent an exact cause absent from the audit.
- Distinguish rejected entries from approved sizing caps, including
  `size capped by exposure or available cash`. A zero exposure-rejection count
  does not prove exposure never constrained sizing. HOLDs are not blocked BUYs;
  no-fill/missing-data and unknown reasons must be reported separately.

### Entry paths and realized added trades

Match exact `(symbol, UTC entry_time)`, not position IDs. Report common, A-only,
C-only counts and each group's PnL/PF/average R (common outcomes for A and C
separately because sizing/capital paths may differ). Include open statuses and
avoid treating an open trade as a realized outcome.

For every C-only entry, join the same-symbol, exact-timestamp A decision and flag
whether it was specifically `max open positions reached`. No nearest-time match
or assumption that every C-only trade was an A capacity rejection.

Report how many old blocked opportunities C captured, their **actual C trade
outcomes**, and the exact matches among the slot diagnostic's +5R/+10R/+20R cases.
These diagnostic price labels are POST_HOC_FUTURE_LABEL, not promised strategy
returns; S diagnostics are usable as A context only with preserved A/S parity.
Do not mix repeated hourly opportunities with unique actual C trades or add their
potential R as portfolio profit. Report whether realized added trades contribute
positive or negative PnL descriptively, without automatically selecting a winner.

### Tail dependence

Freeze top-10 winning-trade contribution, exact FET and PEOPLE entry retention,
profit concentration, and fixed-cohort results excluding top 1/top 3/top 5 winners.
FET anchor: 2024-02-24T12:00:00Z; PEOPLE: 2024-05-31T01:00:00Z. Sort closed winners
by net realized USDT PnL with deterministic symbol/entry-time tie-breaks. Report
contribution against both gross winning PnL and total net PnL; if net PnL is zero
or negative, flag ratio interpretation and retain absolute USDT amounts. Cohort
exclusions do not simulate the alternative cash/entry path without those trades.

## Validation results and production impact

Backend validation PASS. Exactly two variants PASS. A/C normalized one-parameter
diff PASS. Original A and shared config parity PASS. Import/export roundtrip PASS.
Deterministic backend config hash PASS:

`4e7dcb865d86ee01eef520f319dccf7713391dce23b60ec7ad10fd54345675f3`

No runtime code or schema changes. No new runtime tests needed for config/docs.
Validation only calls pure backend config functions; it never constructs a
ReplayStore, data provider, portfolio, execution service, DB or worker.

Production behavior changed: **NO**. Migration: **NO**. Deployment: **NO**.
Replay started: **NO**. No production settings or service changes.

## Reproduce the machine checks

Run from this research worktree with the project's existing virtual environment:

```sh
PYTHONPATH=. /home/ndvi/crypto-bot/.venv/bin/python - <<'PY'
"""Pure config validation only: no ReplayStore/provider/DB/worker API."""
import copy,json
from pathlib import Path
from crypto_bot.replay.config import validate,input_config,config_hash
path=Path('docs/replay/portfolio-capacity-v1-2024-exploration.config.json')
raw=json.loads(path.read_text());cfg=validate(raw);public=input_config(cfg)
base=validate(json.loads(Path('docs/replay/institutional-entry-ranking-v1-2024-exploration.config.json').read_text()))
def diff(a,b,p=''):
 if isinstance(a,dict) and isinstance(b,dict):
  result=[]
  for k in sorted(a.keys()|b.keys()):
   key=f'{p}.{k}' if p else k
   if k not in a or k not in b:result.append(dict(path=key,A=a.get(k),C=b.get(k)))
   else:result.extend(diff(a[k],b[k],key))
  return result
 return [] if a==b else [dict(path=p,A=a,C=b)]
assert len(cfg['variants'])==2
assert cfg['variants'][0]==base['variants'][0], 'A differs from executable baseline'
assert {k:v for k,v in cfg.items() if k not in ('name','variants')}=={k:v for k,v in base.items() if k not in ('name','variants')}, 'Common config changed'
a,c=copy.deepcopy(public['variants'])
a.pop('name');c.pop('name')
changes=diff(a,c)
assert changes==[dict(path='risk_parameters.max_open_positions',A=3,C=100)],changes
# Internal backend materialization may duplicate the same one control, not
# introduce a second risk setting. Check the FULL internal diff explicitly.
full_diff=diff(*cfg['variants'])
assert full_diff==[
 dict(path='name',A='A - Institutional Baseline Control',C='C - Exposure-Limited Capacity'),
 dict(path='paper_settings.max_open_positions',A=3,C=100),
 dict(path='risk_parameters.max_open_positions',A=3,C=100)],full_diff
for variant in cfg['variants']:
 assert variant['strategy']=='institutional_style_v1'
 assert variant['exit_policy']=='baseline_v1' and variant['exit_parameters']=={}
 assert all(variant.get(k,'OFF')=='OFF' for k in ('entry_ranking','entry_admission','setup_rearm','btc_regime_entry_gate'))
 assert all(v is None for v in variant['entry_filter'].values())
 assert variant['paper_settings']['correlation_filter_enabled'] is True
assert (cfg['start'],cfg['end'])==('2024-01-01T00:00:00Z','2025-01-01T00:00:00Z')
assert cfg['name']=='2024 EXPLORATION - Portfolio Capacity Research v1'
assert cfg['dataset_role']=='EXPLORATION' and cfg['universe_size']==20
assert cfg['stale_position_policy']=='RESEARCH_QUARANTINE_STALE'
assert cfg['fallback_5m'] is False and cfg['conservative'] is True
assert cfg['candidate_symbols'] is None
assert cfg['minimum_quote_volume']=='5000000'
assert cfg['spread_approximation_pct']=='0.02'
assert 'market_context_version' not in raw and 'paper_settings' not in raw['variants'][0]
assert cfg['market_context_version']=='ISOLATED_TOPN_V1'
assert validate(public)==cfg
assert input_config(validate(public))==public
assert config_hash(validate(copy.deepcopy(raw)))==config_hash(cfg)
assert config_hash(validate(json.loads(json.dumps(raw,sort_keys=True))))==config_hash(cfg)
print(json.dumps(dict(backend_validation='PASS',exact_two_variants=True,baseline_A_exact_parity=True,common_config_exact_parity=True,normalized_public_variant_diff=changes,complete_internal_backend_diff=full_diff,import_export_roundtrip='PASS',deterministic_hash='PASS',config_hash=config_hash(cfg),backend_derived_context=cfg['market_context_version'],runtime_change=False,database_access=False,replay_started=False),indent=2))
PY
```

Any failed assertion is a STOP: no push, deployment or replay. The above checks
include the entire internal diff and the public parameter diff; neither additional
parameters nor unexpected derived-setting differences may pass.
