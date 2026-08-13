# -*- coding: utf-8 -*-
"""
시장 데이터(주가·환율) 수집
================================================

EV(기업가치)를 구하려면 시가총액이 필요한데 DART·EDGAR 어디에도 주가는 없다.
야후 파이낸스의 차트 API로 종가를 받아 쓴다.

[왜 차트 API인가]
  야후의 시가총액 조회(quote / quoteSummary)는 2024년 이후 인증(crumb)을 요구해
  그냥 부르면 401로 거절당한다. 반면 차트 API는 인증 없이 열려 있다.
  그래서 시가총액은 받아오지 않고 이렇게 계산한다.

      시가총액 = 종가(야후) × 상장주식수(DART 주식총수 / SEC dei)

  주가와 주식수의 출처가 각각 분명해져서 조서에 근거를 적기도 낫다.

[기준일과 평균]
  기준일 하나의 종가로 볼 수도 있고, 기준일까지의 일정 기간 평균으로도 볼 수 있다.
  단일 종가는 그날의 이상변동에 휘둘리므로 실무에서는 1개월·3개월 평균을 자주 쓴다.
"""

import json
import time
import datetime
import urllib.error
import urllib.parse
import urllib.request


CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"

# 야후는 브라우저가 아닌 요청을 막을 때가 있어 일반적인 UA를 붙인다.
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# 평균 기간 선택지 — (표시명, 달력일수). 1은 '기준일 종가'를 뜻한다.
AVG_CHOICES = [
    ("기준일 종가", 1),
    ("1주 평균", 7),
    ("1개월 평균", 30),
    ("3개월 평균", 90),
]


class MarketError(Exception):
    pass


class MarketClient:
    """야후 파이낸스 차트 API 호출. 같은 조회는 메모리에 담아 두고 재사용한다."""

    def __init__(self, delay=0.2, max_retry=3):
        self.delay = delay
        self.max_retry = max_retry
        self._last_call = 0.0
        self._cache = {}          # (ticker, 기준일, 일수) -> 결과
        self._ticker_cache = {}   # 종목코드 -> 야후 티커

    def _throttle(self):
        gap = time.time() - self._last_call
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last_call = time.time()

    def _get(self, url):
        req = urllib.request.Request(url, headers=HEADERS)
        last_err = None
        for attempt in range(self.max_retry):
            self._throttle()
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                # 404는 그 티커가 없는 것이므로 재시도해도 소용없다
                if e.code == 404:
                    raise MarketError("해당 종목을 찾을 수 없습니다")
                last_err = e
                time.sleep(2 ** attempt)
            except Exception as e:
                last_err = e
                time.sleep(2 ** attempt)
        raise MarketError(f"주가 조회 실패: {last_err}")

    # ── 종목코드 → 야후 티커 ─────────────────────────────
    def korean_ticker(self, stock_code):
        """
        DART의 6자리 종목코드에는 시장 구분이 없다.
        야후는 유가증권 `.KS`, 코스닥 `.KQ` 로 나뉘므로 순서대로 넣어 본다.
        """
        if stock_code in self._ticker_cache:
            return self._ticker_cache[stock_code]

        for suffix in (".KS", ".KQ"):
            ticker = stock_code + suffix
            try:
                self.quote(ticker, datetime.date.today(), 1)
                self._ticker_cache[stock_code] = ticker
                return ticker
            except MarketError:
                continue

        raise MarketError(f"야후에서 종목 {stock_code} 를 찾지 못했습니다")

    # ── 종가 조회 ────────────────────────────────────────
    def quote(self, ticker, as_of=None, avg_days=1):
        """
        기준일(as_of)까지의 종가를 받는다.

        avg_days=1 이면 기준일 이전(당일 포함) 가장 가까운 거래일의 종가,
        그 밖에는 기준일까지 avg_days 달력일 구간의 종가 단순평균이다.
        휴장일에 기준일을 잡아도 직전 거래일이 잡히도록 여유를 두고 조회한다.
        """
        as_of = as_of or datetime.date.today()
        key = (ticker, as_of.isoformat(), avg_days)
        if key in self._cache:
            return self._cache[key]

        # 연휴·장기 휴장을 감안해 최소 12일치는 받는다
        span = max(avg_days, 12) + 5
        end = datetime.datetime.combine(as_of, datetime.time(23, 59))
        period2 = int(end.timestamp())
        period1 = period2 - span * 86400

        url = CHART_URL.format(ticker=urllib.parse.quote(ticker)) + \
            f"?period1={period1}&period2={period2}&interval=1d"
        data = self._get(url)

        result = (data.get("chart") or {}).get("result")
        if not result:
            raise MarketError(f"{ticker}: 주가 자료가 없습니다")
        block = result[0]

        stamps = block.get("timestamp") or []
        closes = ((block.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        series = [(datetime.date.fromtimestamp(t), c)
                  for t, c in zip(stamps, closes) if c is not None]
        series = [(d, c) for d, c in series if d <= as_of]
        if not series:
            raise MarketError(f"{ticker}: 기준일까지의 종가가 없습니다")

        if avg_days <= 1:
            used = series[-1:]
        else:
            first = as_of - datetime.timedelta(days=avg_days - 1)
            used = [(d, c) for d, c in series if d >= first] or series[-1:]

        price = sum(c for _d, c in used) / len(used)
        meta = block.get("meta") or {}
        out = {
            "ticker": ticker,
            "price": price,
            "currency": (meta.get("currency") or "").upper(),
            "name": meta.get("longName") or meta.get("shortName") or "",
            "days": len(used),
            "first_date": used[0][0].isoformat(),
            "last_date": used[-1][0].isoformat(),
        }
        self._cache[key] = out
        return out

    # ── 환율 ────────────────────────────────────────────
    def fx_to_krw(self, currency, as_of=None, avg_days=1):
        """
        해당 통화 1단위가 몇 원인지. 한국 회사와 미국 회사를 한 차트에 놓으려면
        금액을 한 통화로 맞춰야 한다. 원화는 1을 그대로 돌려준다.
        """
        currency = (currency or "KRW").upper()
        if currency == "KRW":
            return {"rate": 1.0, "ticker": "", "last_date": "", "days": 0}

        quote = self.quote(f"{currency}KRW=X", as_of, avg_days)
        return {
            "rate": quote["price"],
            "ticker": quote["ticker"],
            "last_date": quote["last_date"],
            "days": quote["days"],
        }
