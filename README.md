# 모의투자 (Paper Trade) for Herdr

가상 자금 1,000만 원으로 국내 주식을 실시간 시세에 사고파는 [Herdr](https://herdr.dev) 패널입니다. 내 투자 성향을 AI에게 알려 주면, AI가 그 성향대로 포트폴리오를 짜고 장중 30분마다 매매하면서 왜 샀는지 쉬운 말로 남깁니다. 실제 증권 계좌나 실제 돈은 쓰지 않습니다.

A Korean-stock paper-trading panel for Herdr. Teach an AI your investing style, let it build a portfolio and trade virtual money on live KOSPI/KOSDAQ prices, and read why it made each trade.

![모의투자](paper-trade/preview.png)

## 설치

Herdr에서 에이전트에게 이렇게 말하면 됩니다.

```
github.com/dotoridot/paper-trade 의 paper-trade 모드를 추가해 줘
```

직접 설치하려면 `paper-trade/mods/paper-trade` 폴더를 `~/.config/herdr-app/mods/`에 복사하고, 탭으로 엽니다.

```sh
herdr-app check ~/.config/herdr-app/mods/paper-trade
herdr-app do tab.new --name 모의투자 --mod paper-trade
```

필요한 것:

- `python3` (표준 라이브러리만 사용)
- `bash`
- AI 기능을 쓰려면 로그인된 [Claude Code](https://claude.com/claude-code) CLI(`claude`). 없어도 직접 주문은 됩니다.

## 쓰는 법

1. **성향 설정**: 새 탭에서 Claude가 투자 기간, 손절 기준, 종목당 비중, 좋아하는 분야, 종목 고르는 기준 등을 하나씩 묻고 `~/.paper-trade/investor_profile.md`를 써 줍니다. 엔진이 직접 지키는 한도는 `strategy.json`과 `rules.json`에 들어갑니다.
2. **계획 세우기**: AI가 성향대로 시장 전체를 훑어 단기와 중기 포트폴리오를 만듭니다. 10~40분 걸립니다. 엔진이 비중 한도, 제외 종목, 재무 기준을 검사한 뒤 확정합니다.
3. **지금 판단** 또는 **자동 판단**: 정규장(평일 09:00~15:30)에 AI가 계획대로 사거나 팔거나 유지하고, 판단마다 기록을 남깁니다. 자동 판단은 기본으로 꺼져 있고, 켜면 장중 30분 남짓마다 한 번 돕니다.
4. 직접 주문도 됩니다. 호가를 누르면 지정가로 들어가고, 정규장에만 체결됩니다. 시간외 거래는 다루지 않습니다.

## 알아 둘 점

- **투자 권유가 아닙니다.** AI의 판단은 틀릴 수 있고, 모의 결과는 실제 수익을 보장하지 않습니다.
- AI 기능은 내 Claude 사용량을 씁니다. 계획 한 번이 수십 번의 도구 호출을 합니다.
- 시세는 네이버 증권의 공개 JSON 주소에서 받습니다. 공식 API가 아니어서 바뀌거나 막힐 수 있습니다.
- 국내 주식만 다룹니다. 수수료 0.015%, 매도세 0.20%, 시장가 슬리피지 0.05%를 반영합니다.
- 패널은 `python3`, `bash`, `claude`를 실행하고 네트워크에 접속합니다. 데이터는 `~/.paper-trade`(또는 `$PAPER_HOME`)에만 씁니다.
- Claude 모델을 정하려면 `PAPER_MODEL` 환경 변수를 씁니다. 없으면 `claude` 기본 설정을 따릅니다.

## 터미널에서 쓰기

엔진은 패널 없이도 동작합니다.

```sh
E=~/.config/herdr-app/mods/paper-trade/engine
python3 -I $E/paper.py search 하이닉스
python3 -I $E/paper.py buy 000660 1 --limit 1300000
python3 -I $E/paper.py status
bash $E/run.sh onboard   # 성향 대화
bash $E/run.sh plan      # 계획
bash $E/run.sh decide    # 판단 한 번
bash $E/run.sh start plan  # 백그라운드로 계획
```

테스트: `cd $E && python3 -m unittest test_paper`

## 구성

```
paper-trade/
  RECIPE.md              다른 에이전트가 이 패널을 처음부터 만들 수 있는 설명
  preview.png
  mods/paper-trade/
    main.js, mod.json    Herdr 패널
    engine/paper.py      시세, 원장, 체결, 계획 검사
    engine/run.sh        AI 실행 (claude CLI)
    engine/prompts/      성향 대화, 계획, 판단 프롬프트
    engine/test_paper.py
```

## License

MIT
