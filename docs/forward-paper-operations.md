# Legacy forward paper 운영 계약 (`paper_forward_v2`)

> 이 문서는 기존 `paper_forward_v2` run의 운영 의미를 보존한다.
> 새 `q1_math_core_v1`의 수학·PIT·arm·risk·order·settlement 계약은
> [q1-math-core.md](q1-math-core.md)를 따른다. 두 경로의 결과를 한
> 성과 계열로 합치거나 기존 run ID의 알고리즘 의미를 변경하지 않는다.

## 버전 선택과 초기화 차이

| 구분 | `paper_forward_v2` | `q1_math_core_v1` |
|---|---|---|
| 선택 | 기본값 또는 명시 selector | 반드시 명시 selector |
| 상속 포지션 | 기존 shadow 주문 arm도 계좌 복제에서 출발 | HOLD·LIVE-MIRROR만 상속 |
| 전략 arm 초기화 | inherited transition | common T0 NAV의 cash-only |
| AI 비교 | B3-RISK − B0-VOL은 AI+손실가드 결합 | Q1-LLM − Q1-DET만 LLM 단독 |
| 주문 pending | legacy decision/attempt 계약 | append-only order event reducer |
| 강제축소 | legacy effect payload와 고정 목표 | typed risk episode와 target generation |
| 현금 | legacy cash와 당일 매도대금 차단 | settled cash와 dated receivable |
| 실행 lane | 내부 PaperBroker만 | 내부 matched arm + 선택적 단일계좌 `ALPACA_PAPER_CANARY` |

```powershell
# 기존 경로
uv run python -m trading.cli paper init `
  --run-id paper_legacy_YYYYMMDD_v1 `
  --algorithm-version paper_forward_v2

# 새 Q1 경로
uv run python -m trading.cli paper init `
  --run-id paper_q1_YYYYMMDD_v1 `
  --algorithm-version q1_math_core_v1
```

`paper init|status|tick|serve`는 같은 run을 이어갈 때 항상 같은
`--algorithm-version`을 사용한다. 생략 시 기본값은
`paper_forward_v2`이며, 이미 생성된 run과 selector가 다르면 실행을
거부한다.

## 목적과 해석

이 legacy 런타임은 사용자가 제공한 계좌 스냅샷을 공통 T0로 복제한 뒤 여러
독립 arm을 같은 시장 데이터와 비용 계약으로 비교합니다. 실제 브로커
주문은 없으며 `real_order_routing=false`입니다.

`TRADING_Q1_ALPACA_PAPER_ENABLED`가 환경에 있더라도 legacy run은 Alpaca
Paper 주문을 만들지 않는다. 기존 run의 의미를 외부 canary 실행으로
자동 변경하지 않는다.

첫 목적은 수익 보장이 아니라 다음을 forward 데이터로 검증하는 것입니다.

- 기준선이 누출 없이 반복 실행되는가
- 정책·리스크·주문·체결·원장이 재현 가능한가
- 비용을 뺀 B3-RISK가 동일 core의 B0-VOL과 장기간 어떻게 다른가
- 실패·재시작·중복 cycle에서 경제적 효과가 한 번만 반영되는가

## Alpaca Paper canary는 Q1 전용

`ALPACA_PAPER_CANARY`는 `q1_math_core_v1`에서만 선택할 수 있는 별도
관측 lane이다. 기본값은 비활성이며 기본 source arm은 `Q1-LLM` 하나다.
Alpaca Paper 계좌는 단일 현금·포지션 원장이므로 여러 독립 arm을 동시에
라우팅하지 않는다.

- 자격증명은 `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` 환경변수에서만 읽는다.
- endpoint는 정확히 `https://paper-api.alpaca.markets`만 허용한다.
- 전용 clean Paper 계좌의 bind·readiness·reconciliation이 통과해야 한다.
- QQQ·SOXX whole-share limit/day 주문만 정규장에 허용한다.
- canary 계좌 return과 fill은 legacy/Q1 matched attribution에 포함하지 않는다.
- `real_order_routing=false`와 live endpoint 차단은 그대로 유지한다.

활성화 전에는 노출되거나 재사용된 키를 폐기하고 새 Paper key를 로컬
환경에만 넣는다. 그 뒤 새 run ID로 다음 순서를 사용한다.

```powershell
uv run python -m trading.cli config validate --all
uv run python -m trading.cli db upgrade
uv run python -m trading.cli paper init --algorithm-version q1_math_core_v1 --run-id <new-run-id>

$env:TRADING_Q1_ALPACA_PAPER_ENABLED = "true"
uv run python -m trading.cli paper serve --algorithm-version q1_math_core_v1 --run-id <new-run-id>
```

`APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`는 실행 전에 별도 비밀 환경 또는
Git에서 제외된 로컬 `.env`에 설정한다. 명령 인자, YAML, 문서나 task
메시지에는 값을 넣지 않는다. 최초 동기화가 clean-account 조건을
통과하기 전에는 Paper 주문을 만들지 않는다.

Paper matcher는 실제 market impact, latency slippage, queue position,
표시 유동성과 규제비용을 재현하지 않는다. 활성화·비동기 취소·fill
reconciliation·UI 상태 계약은 [q1-math-core.md](q1-math-core.md)의
`ALPACA_PAPER_CANARY` 절을 따른다.

## Arm 계약

이 표는 legacy 전용이다. Q1에서는 HOLD와 LIVE-MIRROR만 상속 포지션을
가지며 B0/Q1 전략 arm은 immutable evaluation anchor의 동일 T0 NAV를
현금으로 받아 빈 포지션에서 시작한다.

| Arm | 15:45 목표 | AI 정책 | 결정론적 손실가드 | 주문 |
|---|---|---|---|---|
| B0-CASH | USD cash | 없음 | 없음 | 활성 |
| B0-QQQ | QQQ 100% | 없음 | 없음 | 활성 |
| B0-VOL | 20일 QQQ 변동성 목표 12% | 없음 | 없음 | 활성 |
| B3-RISK | B0-VOL core | reduce-only | 활성 | 활성 |
| B1/B2/B3-FULL | 준비 상태 관측 | 미적용 | 미적용 | 비활성 |
| HOLD | 원계좌 유지 | 없음 | 없음 | 비활성 |

모든 주문 arm은 같은 기존 보유분에서 시작합니다. 하루 한 방향 turnover
한도는 세션 시작 NAV의 25%이므로 목표 포트폴리오로 여러 날에 걸쳐
이행할 수 있습니다. 비핵심 포지션이 남은 동안 성과 창은
`INHERITED_ACCOUNT_TRANSITION`입니다. B0-VOL과 B3-RISK가 비핵심
포지션 제거·pending 없음·최신 목표 5%p 이내를 모두 충족하면
`MATCHED_COMPARISON_READY_FROM_T0_WITH_TRANSITION_HISTORY`로 바뀌지만,
T0 이후 이행 비용과 수익률 이력은 그대로 포함합니다.

기존 `NVDA`, `TSM`, `KLAC`, `LRCX`, `MU`, `SOXL`은 매도·축소만
허용합니다. SOXL 신규매수와 SOXS 방향성 주문은 현재 생성하지 않습니다.

## 세션 순서

1. Alpaca 거래소 캘린더로 세션 cycle을 버전화합니다.
2. 09:30 ET 뒤 모든 원계좌 보유종목의 fresh common quote로 T0를
   확정합니다. 30분 안에 공통 quote가 없으면 bootstrap을 실패 처리합니다.
3. 정규장 동안 15분마다 모든 arm NAV를 기록하고 B3 손실가드를 평가합니다.
   11:00·14:00·15:45에도 이 결정론적 가드는 commander 응답을 기다리지
   않고 먼저 실행합니다.
4. 매시간 최대 4시간 뉴스 창을 WebGPT 5.6 Sol xhigh가 구조화합니다.
5. 11:00·14:00 ET에 선택된 commander가 제한된 B3 정책을 검토합니다.
   오류·타임아웃·NO_CHANGE는 기준선과 손실가드에 영향을 주지 않습니다.
6. 15:45 ET에는 느린 commander 호출을 기다리지 않고 15:45까지 생성된
   마지막 유효 정책과 직전 완료 일봉으로 네 주문 arm을 리밸런싱합니다.
   signal·policy cutoff는 15:45로 고정하고, portfolio state와 실행 가능
   quote는 실제 주문 준비시각을 별도 기록합니다.
7. 정규장 중 매분 pending intent를 주문 뒤 처음 관측된 실행 가능 IEX
   quote에 맞춰 내부 체결합니다.
8. 16:15 ET 보고, 17:30 ET 원장/NAV 대조 cycle을 기록합니다.

## 데이터와 체결

- QQQ 변동성: adjusted-all 완료 일봉 21개로 20개 수익률 계산
- 당일 부분 일봉: 뉴욕 세션 날짜가 같은 일봉은 종가와 ADV에서 제외
- 신선도: 결정 quote 15초, quote bundle skew 20초
- 가격: BUY ask, SELL bid, 주문 뒤의 quote만 허용
- 수량: 남은 수량, 표시 호가 참여율 10%, arm·symbol·세션 누적 기준
  20일 IEX ADV의 2.5% 잔여분 중 최소
- 가격가드: 결정 reference에서 100 bp 이상 불리하면 대기
- 비용: quote crossing + 1 bp delay + 토스 수수료 가정
- 수수료: 미국주식 0.1%, 주문 누적 체결금액 10달러 이하 면제
- 현금결제: 당일 매도 순대금은 같은 세션 BUY 가능 현금에서 제외
- 규제·세금·FX: 아직 공식 adapter로 검증되지 않아 별도 0 버전

IEX는 단일 거래소이며 SIP/NBBO가 아닙니다. 이 체결은 실제 토스 체결의
대체물이 아니라 일관된 보수적 paper 모델입니다.

## B3-RISK 경계

LLM은 주문·수량·raw weight를 출력할 수 없습니다. 허용되는 효과는 최대
6시간의 버전화된 위험축소 정책뿐입니다.

- 전체 risk multiplier 축소
- QQQ 또는 허용된 미국주식/기술 beta factor 신규진입 차단
- 기본 정책 복원

정책 만료는 새 append-only 복원 버전을 만듭니다. 정책 조회는
`effective_from/expires_at`뿐 아니라 패치 실제 생성시각도 검사하므로,
나중에 생성된 소급 effective patch가 과거 결정에 섞이지 않습니다.
자동 요청은 cycle lease owner와 attempt가 일치해야 반영됩니다.

결정론적 손실가드는 B3-RISK에만 적용합니다.

- 일중 손실 1.5%: 신규 BUY 차단
- T0 이후 peak 대비 drawdown 8%: 신규 BUY 차단
- 일중 손실 2.5%: 신규 BUY 차단과 QQQ core 50%를 포함한 축소 목표
- peak 대비 drawdown 12%: leverage 전량, 반도체 cluster cap, QQQ core
  순서의 종목별 축소 목표

최초 hard-loss 발동 때 종목별 목표 수량을 고정해 같은 손실 episode 동안
재사용합니다. 따라서 15분마다 현재 QQQ 잔량의 50%를 다시 줄이지 않으며,
QQQ 축소 예산을 SGOV 같은 다른 surplus 종목이 대신 소비할 수 없습니다.
가드가 정상·soft 상태로 회복된 기록이 생기면 latch를 해제합니다.
세션 시작 NAV나 T0 이후 peak NAV가 없으면 추정값으로 대신하지 않고
결정을 fail-closed합니다.

손실가드가 활성화되면 더 최신 portfolio decision이 이전 pending BUY를
supersede하고, 이미 실행 대기 중인 BUY도 terminal loss-guard attempt로
차단합니다.

## 멱등성·재시작

- cycle과 경제적 effect는 stable ID와 unique constraint를 사용합니다.
- 같은 cycle 재실행은 저장된 결과를 반환하고 다른 manifest로 덮어쓰지
  않습니다.
- 같은 예정시각의 NAV 가드와 AI 결정은 실제 생성시각으로 최신 결정을
  판별해, 느린 commander가 손실가드를 지연시키거나 ID 정렬이 주문을
  뒤집지 못하게 합니다.
- cycle lease owner·attempt·DB clock fence가 stale worker를 거부합니다.
- arm state를 잠근 뒤 최신 portfolio decision을 다시 확인해 superseded
  주문의 race fill을 막습니다.
- 손실가드 terminal cancellation도 arm lock 뒤 다시 확인하며, 충돌한
  cycle은 terminal 실패가 아니라 새 lease attempt로 재시도합니다.
- fill, arm snapshot, ledger transaction, NAV는 한 DB transaction으로
  반영합니다.
- 프로세스 재시작은 같은 run/config/code hash일 때만 이어갑니다. 경제
  코드나 설정이 바뀌면 새 run ID를 사용합니다.

## 성과 판독 제한

- T1/R1/X1은 readiness marker만 기록하며 아직 feature·signal·realization
  기반 OOS challenger가 아닙니다.
- B3-RISK 대 B0-VOL은 AI 정책과 결정론적 손실가드의 합산 효과입니다.
  AI 단독 인과 기여는 아직 분리되지 않습니다.
- matched comparison 준비 표시는 비핵심 포지션 제거, pending 주문 없음,
  마지막 기준선 목표 대비 5%p 이내를 모두 충족해야 켜집니다.
- 완료 거래일의 정확한 연속성은 현재 provenance·가용시각·7일 gap
  fail-closed로 보호하지만, 별도 영속 거래소 캘린더와의 일대일 대조는
  후속 항목입니다.
- 승격 판단에는 충분한 독립 거래, 비용 민감도, 변동성·사건 regime,
  walk-forward/OOS 증거가 추가로 필요합니다.

## 운영 전 체크

```powershell
Set-Location <repository-root>
.\.venv\Scripts\python.exe -m trading.cli db upgrade
.\.venv\Scripts\python.exe -m trading.cli config validate --all
.\.venv\Scripts\python.exe -m trading.cli doctor
.\.venv\Scripts\python.exe -m trading.cli paper init --run-id paper_20260728_v4
.\.venv\Scripts\python.exe -m trading.cli paper serve `
  --run-id paper_20260728_v4 `
  --host 127.0.0.1 `
  --port 8765 `
  --enable-ai
```

UI는 `http://127.0.0.1:8765`이고, 재가동 전에는 포트 8765에 기존 listener가
없는지 확인합니다. `.env`, `.local`, `data/raw`는 Git에 포함하지 않습니다.
