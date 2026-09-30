Option Strike Selector — Final Master Plan

1. Project Objective

Build a real, production-oriented Options Strike Selection Decision-Support System that runs on an Android 14 mobile device.

The system will:

1. Connect to Angel One SmartAPI as the primary market-data source.
2. Use Jugaad-Data / "jugaad_data" as a secondary/fallback/enrichment source wherever required data is not reliably available from Angel One.
3. Never fabricate or estimate missing market data as if it were real.
4. Run continuously in 1-minute cycles during the configured market session.
5. Analyse a defined set of 10 candidate option strikes.
6. Rank those 10 strikes using real market data and quantitative calculations.
7. Select the single highest-quality strike only when minimum signal-quality conditions are satisfied.
8. Provide:
   - Best strike
   - CE/PE
   - Entry / Entry Zone
   - Stop Loss
   - Target
   - Risk/Reward
   - Score
   - Confidence
   - Data quality
   - Reasons for selection
9. Track every cycle and compare the current result with previous cycles.
10. Detect whether a previously selected strike is:
    - strengthening,
    - weakening,
    - confirmed,
    - invalidated,
    - or repeatedly failing.
11. Maintain historical performance of signals.
12. Never automatically place an order.
13. Keep all credentials, API keys, tokens, IDs and passwords outside source code in ".env".
14. Be designed specifically for Android 14 deployment.

---

2. Important Design Principle

The system is not a guaranteed prediction system.

It does not claim:

«"This strike has an 87% guaranteed chance of profit."»

Instead:

«"Confidence" = calibrated signal-quality score based on measurable current and historical evidence.»

The system must be allowed to output:

NO CLEAR STRIKE

when the available evidence is insufficient.

This is preferable to forcing a strike recommendation every minute.

---

3. High-Level Architecture

ANDROID 14
   |
   v
Mobile Runtime
   |
   +-----------------------+
   |                       |
   v                       v
Angel One SmartAPI     Jugaad-Data
PRIMARY                FALLBACK/ENRICHMENT
   |                       |
   +-----------+-----------+
               |
               v
       DATA NORMALIZER
               |
               v
       DATA VALIDATOR
               |
               v
       MARKET DATA CACHE
               |
               v
       MARKET ANALYSIS
               |
               +---- Underlying Trend
               +---- Momentum
               +---- Volatility
               +---- Option Chain
               +---- OI
               +---- OI Change
               +---- Volume
               +---- IV
               +---- Greeks
               +---- Liquidity
               +---- Spread
               +---- Support/Resistance
               |
               v
       10 STRIKE CANDIDATES
               |
               v
        SCORING ENGINE
               |
               v
       CONFIDENCE ENGINE
               |
               v
      PREVIOUS-CYCLE ENGINE
               |
               v
        RISK ENGINE
               |
               +---- Entry
               +---- SL
               +---- Target
               +---- Risk/Reward
               |
               v
        BEST STRIKE
               |
               v
        SQLITE HISTORY
               |
               v
       TERMINAL / MOBILE UI

---

4. Data Sources

4.1 Primary Source — Angel One SmartAPI

Angel One SmartAPI will be treated as the primary source for live market information.

The architecture will support:

- authentication
- instrument/scrip master
- option contract identification
- WebSocket streaming
- live quotes
- LTP
- bid
- ask
- volume
- open interest
- required market fields available from the API

Angel One has documented option-chain data through WebSocket streaming, using the relevant instrument tokens from the Scrip Master.

The implementation must use the current SmartAPI documentation/API behaviour at development time, rather than relying on old examples.

---

5. Secondary Source — Jugaad-Data

Jugaad-Data will be used for:

1. Missing fields.
2. Option-chain enrichment.
3. Cross-checking selected data.
4. Fallback when the primary source is temporarily unavailable.
5. Data fields that are easier to obtain from the NSE option-chain representation.

Jugaad-Data provides live option-chain functionality including index/equity option-chain data.

Example conceptual flow:

Angel LTP      -> PRIMARY
Angel OI       -> PRIMARY
Angel Volume   -> PRIMARY

Jugaad IV      -> SECONDARY
Jugaad Chain   -> SECONDARY

If Angel field missing:
    use Jugaad

If both available:
    compare / validate

---

6. Data Provenance

Every important value should carry its source.

Example:

LTP:
    value = 142.50
    source = ANGEL
    timestamp = ...

OI:
    value = 185000
    source = ANGEL
    timestamp = ...

IV:
    value = 13.82
    source = JUGAAD
    timestamp = ...

The system must never silently replace one source with another.

If two sources disagree materially:

DATA CONFLICT

should be generated.

Confidence should be reduced when important data conflicts occur.

---

7. No Fabricated Data Rule

This is mandatory.

If a value is unavailable:

value = NULL

not:

value = guessed_value

For example:

IV unavailable

must not become:

IV = estimated 14%

unless a clearly documented mathematical calculation is being performed from valid inputs.

The system must distinguish between:

OBSERVED
CALCULATED
DERIVED
MISSING
STALE
CONFLICTED

---

8. Instruments — Phase 1

The first production implementation should focus on:

NIFTY

with:

NIFTY CE
NIFTY PE

After the pipeline is proven:

BANKNIFTY
FINNIFTY
other supported instruments

can be added.

The instrument architecture must nevertheless be configurable so that adding another underlying does not require rewriting the entire system.

---

9. Expiry Selection

The system must explicitly identify:

Underlying
Expiry
Strike
Option Type
Token
Trading Symbol
Lot Size

The expiry must never be inferred incorrectly from symbol text alone.

Configuration:

EXPIRY_MODE=current_weekly

or another explicitly supported mode.

The selected expiry must be displayed in every signal.

---

10. Candidate Strike Generation

The system first determines:

Spot Price
ATM Strike
Strike Interval

Example:

Spot = 25083
ATM = 25100
Strike interval = 50

The candidate universe can then be constructed around ATM.

However, the system should not simply choose arbitrary 10 strikes.

The candidate-generation process should consider:

- ATM distance
- liquidity
- volume
- OI
- spread
- option availability
- expiry
- CE/PE
- current underlying direction

---

11. Ten-Strike Selection

The system must ultimately analyse exactly 10 valid candidate contracts whenever sufficient market data exists.

Example:

1. 25000 CE
2. 25050 CE
3. 25100 CE
4. 25150 CE
5. 25200 CE

6. 25000 PE
7. 25050 PE
8. 25100 PE
9. 25150 PE
10. 25200 PE

The actual ten contracts will be dynamically determined according to the current ATM and market regime.

The system should reject contracts that fail minimum data-quality/liquidity rules.

If ten valid contracts cannot be obtained:

INSUFFICIENT VALID CANDIDATES

and no forced recommendation should be produced.

---

12. Market Regime Engine

Before selecting a strike, the underlying market must be classified.

Possible states:

BULLISH
BEARISH
NEUTRAL
HIGH_VOLATILITY
LOW_VOLATILITY
BREAKOUT
BREAKDOWN
CHOPPY
NO_CLEAR_REGIME

Inputs may include:

- price trend
- short-term returns
- VWAP
- moving averages
- ATR/volatility
- recent highs/lows
- momentum
- volume
- market breadth if available
- option-chain structure

The purpose is not to predict the market by one indicator.

It is to prevent selecting a CE when the broader evidence is strongly bearish, or selecting a PE when the evidence is strongly bullish.

---

13. Per-Strike Data

For every candidate strike, collect as many real fields as available:

Option Market Data

LTP
Bid
Ask
Bid Quantity
Ask Quantity
Volume
Open Interest
Change in OI
Previous Close

Derived Market Data

Premium Change
1-minute Return
3-minute Return
5-minute Return
Momentum
Rate of Change
Relative Volume
Spread
Spread %
Distance from ATM

Volatility

IV
IV change
IV percentile/rank if sufficient historical data exists
Underlying volatility

Greeks

Where available:

Delta
Gamma
Theta
Vega

If Greeks are not supplied reliably, they may be mathematically derived only when the required inputs are valid.

---

14. Liquidity Filter

Liquidity is a hard requirement.

The system should check:

OI
Volume
Bid
Ask
Spread
Spread %

Very illiquid contracts should either:

REJECT

or receive a severe score penalty.

High OI can be useful for liquidity and fill-quality considerations, but OI must not be interpreted by itself as proof of direction. Higher-OI strikes can generally have better liquidity/tighter spreads, while low-OI contracts can have poorer execution quality.

---

15. Core Scoring Engine

Every candidate receives a normalized score from:

0 to 100

Initial architecture:

Underlying Trend             20
Option Momentum              15
OI + OI Change               15
Volume / Relative Volume     10
Liquidity / Spread           10
Strike / ATM Suitability     10
Greeks                       10
IV Behaviour                  5
Risk/Reward                   5
--------------------------------
TOTAL                       100

These are initial weights only.

They must be validated and tuned through historical walk-forward testing.

---

16. Underlying Trend Score

The underlying component may combine:

1-minute direction
3-minute direction
5-minute direction
VWAP relationship
short-term moving-average structure
recent high/low structure
momentum
volatility regime

Example:

Strong bullish underlying
+
CE candidate
=
positive directional score

Strong bullish underlying
+
PE candidate
=
directional penalty

The penalty should not be absolute because options can behave differently under volatility/positioning conditions.

---

17. Momentum Score

Momentum should use more than the latest LTP.

Example:

1-minute return
3-minute return
5-minute return
acceleration
underlying-option correlation
relative volume

A strike whose premium is rising while the underlying is moving in the expected direction receives stronger confirmation.

---

18. OI + OI Change Score

Track:

Current OI
Previous OI
OI change
OI change %

Also compare the candidate's OI behaviour against nearby strikes.

Do not use:

HIGH OI = BUY

as a rule.

Instead:

OI
+
OI Change
+
Price
+
Volume
+
Underlying Direction

must be interpreted together.

---

19. Volume Score

Calculate:

Current volume
Volume acceleration
Relative volume
Volume vs recent average

A sudden increase in volume accompanied by price and underlying confirmation is stronger than volume alone.

---

20. Spread Score

Calculate:

spread = ask - bid

spread_percent =
    (ask - bid) / mid_price * 100

Poor spread:

penalty

Extreme spread:

candidate rejected

This prevents selecting a theoretically attractive but practically difficult option.

---

21. ATM Distance Score

The system should account for:

ATM
ITM
ATM
OTM

Extremely far OTM strikes should receive penalties unless the underlying move and option behaviour provide unusually strong evidence.

The objective is:

Probability
+
Responsiveness
+
Liquidity
+
Risk/Reward

rather than simply choosing the cheapest premium.

---

22. Greeks Score

Where reliable Greek data is available:

Delta
Gamma
Theta
Vega

should be incorporated.

The system should favour a Greek profile consistent with the intended directional move.

Example:

Strong bullish setup
+
reasonable Delta
+
useful Gamma
+
acceptable Theta
=
stronger score

Greeks must not be treated independently from IV and expiry.

---

23. IV Score

Track:

Current IV
IV change
IV relative to recent values

A rising option premium caused only by IV expansion is different from a directional move supported by underlying movement.

Therefore:

Premium ↑
Underlying ↑
IV ↑

must be interpreted differently from:

Premium ↑
Underlying flat
IV ↑↑

---

24. Risk/Reward Engine

The system must calculate a proposed:

Entry
Stop Loss
Target
Risk
Reward
Risk/Reward Ratio

It must not use one universal percentage for all market conditions.

---

25. Entry Calculation

Entry should be based on:

Current bid
Current ask
Mid price
Recent premium structure
Underlying trigger
Momentum confirmation
Breakout/retest conditions

For a buy setup:

Entry Zone

is preferable to a false exact-price prediction.

Example:

Current:
LTP = 142

Entry Zone:
140 - 144

Confirmation:
Underlying remains above trigger
and option momentum remains positive

---

26. Stop-Loss Calculation

SL should be derived from the invalidation of the setup.

Possible inputs:

Underlying technical invalidation
Option premium volatility
ATR/volatility
Recent option swing low
Spread/liquidity

The system should expose the reason:

SL Reason:
Underlying support invalidation

or:

SL Reason:
Volatility-adjusted premium stop

---

27. Target Calculation

Target should combine:

Expected underlying movement
Option responsiveness
Volatility
Resistance/support
Risk/Reward

Minimum acceptable R:R can be configurable:

MIN_RR = 1.5

or another validated value.

If a valid target cannot be calculated:

NO VALID TARGET

and the candidate should not become BEST.

---

28. Confidence Engine

Confidence should be a separate layer from raw score.

Example:

RAW SCORE = 86.5

but:

Data Quality = 96
Historical Evidence = 72
Current Confirmation = 91

may result in:

CONFIDENCE = HIGH

Confidence should consider:

Signal score
Data quality
Source agreement
Liquidity
Market regime clarity
Historical setup performance
Previous-cycle consistency
Risk/Reward

---

29. Confidence Bands

Initial display:

90–100 = VERY HIGH
80–89  = HIGH
70–79  = MEDIUM
60–69  = LOW
<60    = NO CLEAR SIGNAL

These thresholds are configurable and must be validated through backtesting.

They are not probabilities.

---

30. Cycle Engine

The system operates on a one-minute cycle.

Example:

10:21:00
10:22:00
10:23:00
10:24:00
...

It must synchronize to minute boundaries instead of blindly doing:

sleep(60)

This avoids timing drift.

---

31. Every Cycle

Each cycle follows:

1. Check market session
2. Check connectivity
3. Update live data
4. Validate timestamps
5. Update underlying
6. Update option chain
7. Generate candidates
8. Select 10 valid strikes
9. Calculate metrics
10. Calculate score
11. Compare previous cycle
12. Calculate entry
13. Calculate SL
14. Calculate target
15. Calculate R:R
16. Calculate confidence
17. Apply filters
18. Select BEST or NO CLEAR STRIKE
19. Save to SQLite
20. Print result
21. Wait for next cycle

---

32. Previous-Cycle Memory

This is a mandatory component.

Every cycle must save:

cycle_id
timestamp
underlying
spot
expiry
strike
option_type
LTP
score
confidence
entry
SL
target
market_regime
data_quality
signal_state

---

33. Same Strike in Consecutive Cycles

Example:

Cycle 1

25100 CE

Score: 78
Confidence: MEDIUM
State: NEW

Cycle 2

25100 CE

Score: 84
Previous: 78
Change: +6

Momentum: UP
Volume: UP
OI: UP
Underlying: CONFIRMED

State: STRENGTHENING
Confidence: HIGH

The system should explicitly show this.

---

34. Weakening Signal

Example:

Cycle 1:
25100 CE
Score 84

Cycle 2:
Score 67

Cycle 3:
Score 58

Output:

25100 CE

Previous Score: 67
Current Score: 58

TREND: WEAKENING

State: INVALIDATING

The system should not continue showing the strike as BEST simply because it was BEST previously.

---

35. Signal State Machine

Each signal can have:

NEW
CONFIRMED
STRENGTHENING
STABLE
WEAKENING
INVALIDATED
TARGET_REACHED
SL_REACHED
EXPIRED

Transitions are based on objective rules.

---

36. Repeated-Selection Control

A strike can remain BEST across multiple cycles if the evidence remains strong.

The system must not artificially change strikes simply to create variety.

However, repeated selection should be accompanied by:

Previous Score
Current Score
Score Change
Previous Confidence
Current Confidence
Signal State

This distinguishes:

legitimate persistence

from:

stale/repeated signal

---

37. Historical Strike Performance

The database should record signal outcomes.

Example:

25100 CE

Signals: 42
Target reached: 19
SL reached: 13
Invalidated: 10

Historical setup score:
67/100

This should not mean:

25100 CE is always good

because market regimes change.

---

38. Regime-Aware History

Historical performance should be segmented by:

Underlying
Option type
Strike distance
Expiry type
Market regime
Time of day
Volatility regime
Setup type

Example:

25100 CE

Bullish + High Momentum:
Strong historical performance

Neutral + Low Momentum:
Weak historical performance

This is much more useful than simple strike-level history.

---

39. Time-of-Day Analysis

The system should maintain separate statistics for:

09:15–10:00
10:00–11:00
11:00–12:00
12:00–13:00
13:00–14:00
14:00–15:00
15:00–15:30

Exact market-session times must be configurable.

This allows the engine to detect different intraday behaviour.

---

40. Historical Outcome Engine

When a signal is generated:

signal_time
entry
SL
target

are frozen.

Future market data determines:

TARGET_REACHED
SL_REACHED
TIMEOUT
INVALIDATED

The system must never modify the original signal using future data.

---

41. Look-Ahead Bias Prevention

Historical testing must use only data that would actually have been available at that point in time.

No future:

OI
price
IV
volume

may leak into a historical decision.

Walk-forward validation should be used instead of simply optimizing on the entire historical dataset. This prevents the system from appearing successful only because it was tuned to information from the future.

---

42. Backtesting

Before trusting live output:

Historical minute data
        |
        v
Same production engine
        |
        v
10 strikes
        |
        v
BEST
        |
        v
Entry/SL/Target simulation
        |
        v
Outcome

Measure:

Win rate
Loss rate
Profit factor
Average R
Maximum drawdown
Average holding time
Target hit rate
SL hit rate
False signal rate
No-signal rate

---

43. Walk-Forward Validation

Recommended process:

Training Period
       |
       v
Parameter Selection
       |
       v
Out-of-Sample Period
       |
       v
Performance
       |
       v
Move Window Forward
       |
       v
Repeat

Weights should not be changed simply because one day's results were poor.

---

44. Transaction Costs and Slippage

Backtesting must include realistic:

Brokerage
Taxes/charges where applicable
Bid-ask spread
Slippage

Otherwise the result may look profitable on paper but fail in real execution.

---

45. Data Quality Engine

Each cycle receives:

DATA QUALITY SCORE

Example:

Angel connectivity:      PASS
Jugaad connectivity:     PASS
LTP freshness:            PASS
OI freshness:             PASS
Bid/Ask:                  PASS
Source agreement:         PASS

Data Quality: 97/100

---

46. Stale Data Rule

If critical data is too old:

STALE DATA

The system must not produce a high-confidence signal.

Possible result:

NO SIGNAL

Reason:
Option-chain data stale by 18 seconds

The exact stale threshold must be configurable by field/source.

---

47. API Failure Handling

If Angel One disconnects:

Angel DOWN
      |
      v
Attempt reconnect
      |
      v
If valid fallback data available
      |
      v
Continue with reduced confidence

If critical data cannot be verified:

NO SIGNAL

not:

use old data forever

---

48. Source Priority

Default:

Angel One
   >
Jugaad-Data
   >
Calculated/derived values

But source priority must be field-specific, because different sources may expose different information more reliably.

---

49. Mobile Runtime

Target:

Android 14

The initial implementation should be designed around a mobile-compatible Python runtime such as:

Termux
+
Python

with the possibility of a dedicated Android UI later.

The calculation engine should remain independent from the UI.

Therefore:

Core Engine

must not depend on:

Terminal screen

or:

Android GUI

---

50. Mobile Architecture

Android 14
|
+-- Termux / Python Runtime
|
+-- option_selector/
|
+-- SQLite
|
+-- .env
|
+-- logs/
|
+-- configuration

The system should be able to run from the terminal first.

A graphical Android dashboard can later consume the same engine.

---

51. Project Structure

option_strike_selector/
│
├── app/
│   ├── main.py
│   ├── config.py
│   ├── scheduler.py
│   │
│   ├── data/
│   │   ├── angel.py
│   │   ├── jugaad.py
│   │   ├── normalizer.py
│   │   ├── validator.py
│   │   └── cache.py
│   │
│   ├── market/
│   │   ├── underlying.py
│   │   ├── option_chain.py
│   │   ├── indicators.py
│   │   ├── greeks.py
│   │   └── regime.py
│   │
│   ├── selector/
│   │   ├── candidates.py
│   │   ├── scoring.py
│   │   ├── confidence.py
│   │   └── ranking.py
│   │
│   ├── risk/
│   │   ├── entry.py
│   │   ├── stoploss.py
│   │   └── target.py
│   │
│   ├── history/
│   │   ├── database.py
│   │   ├── cycles.py
│   │   ├── signals.py
│   │   └── outcomes.py
│   │
│   └── output/
│       ├── terminal.py
│       └── dashboard.py
│
├── data/
│   ├── history.db
│   └── logs/
│
├── .env
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md

---

52. Secrets Management

All sensitive values must be stored in:

.env

Examples:

ANGEL_API_KEY=
ANGEL_CLIENT_ID=
ANGEL_PASSWORD=
ANGEL_TOTP_SECRET=

JUGAAD_CONFIG=

Actual names will be finalized according to the APIs used.

Never hard-code credentials in:

.py
.json
.md

or source control.

---

53. Git Security

".gitignore" must contain:

.env
*.db
*.log
__pycache__/
.venv/

A safe template should be provided:

.env.example

containing variable names only.

---

54. No Automatic Trading

This project is strictly:

DECISION SUPPORT

It will not contain automatic:

BUY
SELL
ORDER
MODIFY ORDER
CANCEL ORDER

functionality.

The output is intended for the user to review and make the final decision.

---

55. Main Live Output

Every cycle should print a structured report.

Example:

============================================================
OPTION STRIKE SELECTOR
============================================================

Cycle       : 124
Time        : 10:27:00
Underlying  : NIFTY
Spot        : 25083.20
Expiry      : <CURRENT EXPIRY>
Regime      : BULLISH
Data Quality: 97/100

------------------------------------------------------------
TOP 10 CANDIDATES
------------------------------------------------------------

Rank  Strike       Type   Score
1     25100        CE     86.7
2     25050        CE     82.1
3     25150        CE     78.4
4     25000        CE     74.8
5     25200        CE     72.5
6     25100        PE     61.3
7     25050        PE     59.4
8     25150        PE     55.8
9     25000        PE     53.1
10    25200        PE     49.7

------------------------------------------------------------
BEST STRIKE
------------------------------------------------------------

Symbol      : NIFTY <EXPIRY> 25100 CE
LTP         : 142.00
Entry Zone  : 140 - 144

Stop Loss   : 132
Target      : 162
Risk        : 12
Reward      : 18
R:R         : 1 : 1.50

Score       : 86.7/100
Confidence  : HIGH
Data Quality: 97/100

------------------------------------------------------------
PREVIOUS CYCLE
------------------------------------------------------------

Previous Strike : 25100 CE
Previous Score  : 83.1
Current Score   : 86.7
Change          : +3.6

State           : STRENGTHENING

------------------------------------------------------------
REASONS
------------------------------------------------------------

[+] Underlying bullish
[+] 5-minute momentum positive
[+] Option momentum positive
[+] Volume confirmation
[+] OI confirmation
[+] Acceptable spread
[+] Suitable delta
[+] Valid risk/reward
[+] Previous-cycle signal improving

============================================================

---

56. NO SIGNAL Output

If no candidate passes minimum requirements:

============================================================
NO CLEAR STRIKE
============================================================

Cycle       : 125
Underlying  : NIFTY
Regime      : NEUTRAL

Best Raw Score: 68.2
Required Score: 75

Reason:
- Directional confirmation weak
- Risk/Reward insufficient
- Candidate liquidity acceptable
- No high-quality setup

RESULT:
NO SIGNAL

============================================================

---

57. Minimum Signal Gate

The system should have hard gates before BEST selection.

Example:

Score >= MIN_SCORE
Confidence >= MIN_CONFIDENCE
Data Quality >= MIN_DATA_QUALITY
R:R >= MIN_RR
Spread <= MAX_SPREAD

If any critical gate fails:

NO CLEAR STRIKE

---

58. Top-3 Backup

Although the main output is one BEST strike, the system should retain:

#1 BEST
#2 ALTERNATIVE
#3 ALTERNATIVE

This is useful if the #1 setup deteriorates immediately.

The system should never present the alternatives as equal recommendations.

---

59. Logging

Each cycle should be logged to:

SQLite

and optionally:

CSV

for analysis/export.

Logs should contain enough information to reconstruct:

«Why did the system select this strike at that exact minute?»

---

60. SQLite Tables

Initial schema:

market_snapshots
option_snapshots
cycles
candidate_scores
signals
signal_state_history
signal_outcomes
system_events

---

61. "cycles" Table

Conceptual fields:

cycle_id
timestamp
underlying
spot
expiry
regime
data_quality
best_symbol
best_score
confidence
entry
stop_loss
target
risk_reward
signal_state

---

62. "candidate_scores" Table

Conceptual fields:

cycle_id
symbol
strike
option_type
ltp
bid
ask
volume
oi
oi_change
iv
delta
gamma
theta
vega
momentum_score
oi_score
volume_score
liquidity_score
greeks_score
iv_score
risk_reward_score
total_score
rank

---

63. Signal History

Every BEST signal must receive a unique:

signal_id

Example:

SIG-20260913-102700-25100CE

The signal record must never be overwritten.

Later cycle information should be stored as a new state/history record.

---

64. Outcome Tracking

After a signal is generated, future data determines:

TARGET_REACHED
SL_REACHED
TIMEOUT
INVALIDATED
EXPIRED

The outcome engine must store:

signal_id
entry
highest_after_entry
lowest_after_entry
target
SL
result
time_to_result

---

65. Historical Reliability

The system should calculate setup-level historical statistics.

Example:

Setup Type:
Bullish + ATM CE + Momentum Confirmation

Samples: 312
Target Hit: 174
SL Hit: 103
Timeout: 35

These statistics can contribute to confidence.

Minimum sample requirements must be enforced to avoid overconfidence from tiny samples.

---

66. Confidence Calibration

Do not call:

5 successful signals
out of
5 signals
=
100% confidence

That is statistically unreliable.

Confidence calibration should consider:

sample size
historical success
market regime
recent performance
data quality
signal strength

A small sample should receive a reliability penalty.

---

67. Adaptive Scoring

The scoring weights should initially be fixed.

Later, after sufficient historical data:

Weights

can be optimized using walk-forward validation.

Example:

Momentum:
15 -> 18

OI:
15 -> 12

Liquidity:
10 -> 14

Only validated changes should enter production.

---

68. Anti-Overfitting Rules

Never optimize until:

backtest looks perfect

The system should avoid:

- excessive indicators
- excessive parameters
- changing weights after every losing day
- using future data
- optimizing against the same dataset repeatedly

The goal is robustness, not a perfect historical equity curve.

---

69. Market Session Handling

The workflow should have:

MARKET_OPEN
MARKET_CLOSED
PRE_OPEN
POST_CLOSE

states.

Outside the configured session:

No live strike selection

The system should not run unnecessary API polling while the market is closed unless explicitly configured for data preparation.

---

70. Connection Monitor

The mobile screen should show:

Angel: CONNECTED
Jugaad: CONNECTED
Database: OK
Last Data: 10:27:58
Next Cycle: 10:28:00

If disconnected:

Angel: DISCONNECTED
Reconnecting...

---

71. Rate-Limit Awareness

The implementation must respect the current API rate limits and terms of Angel One and any secondary data source.

The system should avoid:

unnecessary REST calls every second

when a WebSocket stream can maintain the required data.

The 1-minute analysis cycle is separate from the live-data ingestion layer.

---

72. Data Ingestion vs Analysis

This distinction is important.

Data layer

Runs continuously:

WebSocket
    ↓
Live cache

Analysis layer

Runs every minute:

Cache snapshot
    ↓
Calculate
    ↓
Rank

This is superior to reconnecting and downloading the entire option chain every minute.

---

73. Cache

In-memory cache should hold:

latest quote
latest OI
latest volume
latest bid
latest ask
timestamp

SQLite stores historical snapshots.

---

74. Recovery

If the app crashes:

Restart
   ↓
Load configuration
   ↓
Reconnect Angel
   ↓
Reconnect Jugaad if needed
   ↓
Load latest database state
   ↓
Resume cycle

The system should not lose all previous signal history.

---

75. Mobile Resource Management

Android 14 is a mobile OS, so the design must consider:

battery
RAM
network changes
Wi-Fi/mobile-data switching
background restrictions
process termination
screen-off behaviour

The core engine should be lightweight.

Avoid unnecessary:

large ML models
heavy dataframes held forever
high-frequency polling
large local datasets

---

76. Future Android UI

After the core engine is stable, a dedicated dashboard can show:

Market
|
+-- Spot
+-- Regime
+-- Trend
|
Best Strike
|
+-- Strike
+-- CE/PE
+-- Entry
+-- SL
+-- Target
+-- Score
+-- Confidence
|
Cycle History
|
+-- Previous
+-- Current
+-- Change
|
Top 10

The UI must consume the same backend engine instead of implementing separate scoring logic.

---

77. Alert System

Optional future feature:

HIGH-CONFIDENCE SIGNAL

can trigger a local Android notification.

Example:

NIFTY 25100 CE
Score 88.2
Confidence HIGH
Signal strengthening

No order should be placed automatically.

---

78. Important Signal-Change Alert

A useful alert is not only a new BEST signal.

Also alert when:

BEST strike changes

or:

existing BEST signal becomes invalid

or:

confidence crosses configured threshold

---

79. Recommended Signal States on Screen

Use:

NEW
CONFIRMED
STRENGTHENING
STABLE
WEAKENING
INVALIDATED

This makes the one-minute cycle history much easier to understand.

---

80. Example Cycle Progression

10:21
25100 CE
Score 76
NEW

10:22
25100 CE
Score 81
STRENGTHENING

10:23
25100 CE
Score 86
CONFIRMED

10:24
25100 CE
Score 89
STRENGTHENING

10:25
25100 CE
Score 74
WEAKENING

10:26
25050 CE
Score 84
NEW BEST

This is the desired behaviour.

---

81. What the System Must NOT Do

The system must not:

Guarantee profit
Guarantee probability
Fabricate missing data
Automatically place orders
Ignore stale data
Ignore liquidity
Choose a strike just because it is cheap
Choose a strike just because OI is high
Use only one indicator
Force a recommendation every minute
Use future data in historical testing
Hard-code API credentials

---

82. What the System SHOULD Do

It should:

Use multiple independent signals
Cross-check data
Measure data quality
Analyse 10 candidates
Rank objectively
Explain the ranking
Track previous cycles
Track historical outcomes
Penalize unreliable setups
Adapt only after validation
Reject poor setups
Provide entry/SL/target
Show risk/reward
Maintain complete logs

---

83. Development Phases

Phase 1 — Foundation

Build:

Android environment
Python
project structure
.env
.gitignore
configuration
logging
SQLite

---

Phase 2 — Angel One

Implement:

authentication
Scrip Master
instrument lookup
WebSocket
quote cache
reconnect

---

Phase 3 — Jugaad Data

Implement:

option chain
normalization
fallback
cross-check
source tagging

---

Phase 4 — Market Engine

Implement:

spot
ATM
trend
momentum
VWAP
volatility
support/resistance
regime

---

Phase 5 — Option Engine

Implement:

OI
OI change
volume
spread
IV
Greeks
option momentum
liquidity

---

Phase 6 — Ten-Strike Selector

Implement:

candidate generation
10-strike validation
liquidity filtering
CE/PE filtering
expiry filtering

---

Phase 7 — Scoring

Implement:

normalized metrics
weighted score
hard filters
ranking

---

Phase 8 — Risk Engine

Implement:

entry
SL
target
R:R
invalidations

---

Phase 9 — Confidence

Implement:

data quality
signal quality
historical reliability
source agreement
regime clarity
confidence

---

Phase 10 — Cycle Memory

Implement:

previous cycle
score delta
confidence delta
state machine
repeated-strike tracking

---

Phase 11 — Outcome Engine

Implement:

target detection
SL detection
timeout
signal outcome
historical statistics

---

Phase 12 — Backtesting

Implement:

historical replay
walk-forward validation
slippage
costs
performance reports

---

Phase 13 — Live Decision Support

Only after validation:

1-minute live cycles
BEST strike
entry
SL
target
confidence
history
notifications

---

84. Acceptance Criteria

The project is considered ready only when:

Data

[ ] Angel connection works
[ ] Reconnection works
[ ] Jugaad fallback works
[ ] Data source is recorded
[ ] Stale data is detected
[ ] Data conflicts are detected

Analysis

[ ] Underlying analysis works
[ ] Option-chain analysis works
[ ] Ten candidates are generated
[ ] Candidates are scored
[ ] BEST selection works

Risk

[ ] Entry calculated
[ ] SL calculated
[ ] Target calculated
[ ] R:R calculated
[ ] Invalid target causes rejection

History

[ ] Every cycle saved
[ ] Previous cycle compared
[ ] Same strike tracked
[ ] Signal state tracked
[ ] Outcome tracked

Reliability

[ ] No forced signal
[ ] No fabricated data
[ ] No future-data leakage
[ ] Walk-forward validation
[ ] Costs/slippage considered

Security

[ ] Credentials in .env
[ ] .env ignored by Git
[ ] No secret in source

Android

[ ] Works on Android 14
[ ] Reconnect works
[ ] Database survives restart
[ ] Logs survive restart
[ ] Resource usage acceptable

---

85. Final Production Workflow

The final system should behave like this:

START
 |
 v
Check Market Session
 |
 v
Connect / Verify Angel One
 |
 v
Receive Live Data
 |
 v
Cross-check / Enrich with Jugaad
 |
 v
Validate Data
 |
 +---- Invalid? ----> NO SIGNAL
 |
 v
Determine Underlying Regime
 |
 v
Determine ATM
 |
 v
Generate Candidate Universe
 |
 v
Select 10 Valid Strikes
 |
 v
Calculate Metrics
 |
 v
Calculate Individual Scores
 |
 v
Rank 10 Strikes
 |
 v
Calculate Entry / SL / Target
 |
 v
Calculate Risk/Reward
 |
 v
Compare Previous Cycles
 |
 v
Check Historical Reliability
 |
 v
Calculate Confidence
 |
 v
Apply Minimum Signal Gates
 |
 +---- Failed? ----> NO CLEAR STRIKE
 |
 v
BEST STRIKE
 |
 +---- Strike
 +---- CE/PE
 +---- Entry
 +---- SL
 +---- Target
 +---- R:R
 +---- Score
 +---- Confidence
 +---- Data Quality
 +---- Previous Score
 +---- Signal State
 +---- Reasons
 |
 v
Save Everything
 |
 v
Print on Android
 |
 v
Wait for Next Minute
 |
 +--------------------+
                      |
                      v
                  NEXT CYCLE

---

86. Final Philosophy

The objective is not:

Every minute give me a trade.

The objective is:

Every minute analyse the available market evidence
and tell me whether one of the 10 candidate strikes
currently has the strongest validated setup.

Therefore the two legitimate outputs are:

BEST STRIKE

or:

NO CLEAR STRIKE

A high-quality system should be comfortable producing the second result.

The system will therefore prioritize:

Real Data
    >
Data Quality
    >
Multiple Confirmations
    >
Liquidity
    >
Risk/Reward
    >
Historical Evidence
    >
Confidence
    >
Strike Selection

rather than attempting to manufacture a prediction every minute.

---

87. Development Rule

Implementation must proceed one component at a time.

Do not build the entire project as one large script.

Order:

1. Android setup
2. Secrets/config
3. Angel connection
4. Jugaad connection
5. Data normalization
6. Data validation
7. Live cache
8. Option-chain engine
9. Market regime
10. Ten-strike selector
11. Scoring
12. Risk engine
13. Confidence
14. Cycle memory
15. SQLite history
16. Outcome engine
17. Backtesting
18. Walk-forward validation
19. Live dashboard
20. Notifications

Each stage must work before the next stage is added.

---

88. Final Constraint

This project is a decision-support system only.

It will provide:

BEST STRIKE
ENTRY
SL
TARGET
SCORE
CONFIDENCE
REASONS
HISTORY

but will not automatically execute trades.

The user makes the final trading decision.

---