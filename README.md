# claude-stats

터미널에서 실행하는 Claude CLI 토큰 사용량 대시보드.

`~/.claude/stats-cache.json`과 `~/.claude/projects/` 디렉토리를 읽어 세션별 토큰 사용량, 모델 분석, 일별 활동, 시간대 분포, 플랜 한도 현황을 시각화합니다.

![dashboard preview](https://raw.githubusercontent.com/eondcom/claude-stats/main/preview.png)

## 요구 사항

- Python 3.9+
- [Claude Code CLI](https://claude.ai/code) (stats-cache.json 생성 필요)
- `rich` 라이브러리

```bash
pip install rich
```

## 설치

```bash
curl -O https://raw.githubusercontent.com/eondcom/claude-stats/main/claude-stats.py
```

또는 저장소 클론:

```bash
git clone https://github.com/eondcom/claude-stats.git
cd claude-stats
pip install rich
```

## 사용법

```bash
# 대시보드 (기본)
python3 claude-stats.py

# 세션별 토큰 사용량
python3 claude-stats.py --sessions
python3 claude-stats.py -s -n 20   # 최근 20개 세션

# 라이브 모드 (htop 스타일, 60초 자동 갱신)
python3 claude-stats.py --watch
python3 claude-stats.py -w -s       # 세션 뷰 + 라이브 모드

# 플랜 한도 입력 (대화형)
python3 claude-stats.py --set-plan

# 플랜 한도 빠른 입력 (세션%, 주간전체%, Sonnet%)
python3 claude-stats.py --set-plan 10,85,60

# stats-cache.json 재계산
python3 claude-stats.py --rebuild
```

## 화면 구성

### 대시보드 (`기본`)

| 섹션 | 내용 |
|------|------|
| ① Summary | 총 세션·메시지 수, 토큰 합계 (Input / Output / Cache R·W) |
| ② Model Breakdown | 모델별 토큰 사용량 비교 |
| ③ Daily Activity | 최근 14일 메시지·세션·툴 호출 수 |
| ④ Hour Distribution | 24시간 사용 시간대 분포 |
| ⑤ Plan Usage Limits | 세션·주간 플랜 한도 사용률 |

### 라이브 모드 키맵 (`--watch`)

| 키 | 동작 |
|----|------|
| `h` / `?` | 도움말 토글 |
| `1` | Summary 브리핑 |
| `2` | Model Breakdown 브리핑 |
| `3` | Daily Activity 브리핑 |
| `4` | Hour Distribution 브리핑 |
| `5` | 사용량 패턴 분석 |
| `p` | Plan Usage Limits |
| `d` | 대시보드 뷰 |
| `s` | 세션 뷰 |
| `r` | 즉시 새로고침 |
| `q` / `Ctrl+C` | 종료 |

## 플랜 한도 기능

Claude.ai의 사용량 제한 수치를 직접 입력해두면 대시보드에서 게이지로 확인할 수 있습니다.

```bash
# 대화형 입력
python3 claude-stats.py --set-plan

# 빠른 입력: 세션%, 주간(전체)%, 주간(Sonnet)%
python3 claude-stats.py --set-plan 15,72,48
```

입력한 시점의 토큰 스냅샷을 기록해 이후 사용량 변화에 따라 자동으로 퍼센트를 보간합니다.

## 데이터 파일 위치

| 파일 | 역할 |
|------|------|
| `~/.claude/stats-cache.json` | 누적 통계 캐시 (Claude CLI 생성) |
| `~/.claude/projects/` | 세션별 JSONL 로그 (Claude CLI 생성) |
| `~/.claude/plan-limits.json` | 플랜 한도 저장 (이 스크립트 생성) |

## 토큰 지표 설명

| 지표 | 설명 |
|------|------|
| **Input** | 프롬프트 + 컨텍스트 토큰 |
| **Output** | Claude가 생성한 응답 토큰 |
| **Cache R** | 캐시에서 재사용된 토큰 (비용 거의 없음) |
| **Cache W** | 캐시에 새로 기록된 토큰 |
| **Total** | Input + Output + Cache R + Cache W |

## License

MIT
