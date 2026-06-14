"""
Phase 0 spike — THROWAWAY. Not part of the app.

Goal (per README §6 Phase 0): prove I can pull implied-vol / option-chain
data for one symbol and print it, BEFORE designing the app around a source.

The README assumes the IV source is likely IBKR (needs TWS/Gateway running +
login) or a paid feed. This script tests a cheaper candidate FIRST: yfinance,
which exposes Yahoo's per-contract impliedVolatility with no API key and no auth.

What we need for the Volatility panel (README §4 Panel D):
  - implied vol per underlying            -> per-contract IV from the chain
  - IV rank / IV percentile               -> needs HISTORY of an ATM IV series
  - realized / historical vol             -> computable from price history
  - IV - RV spread                        -> derived from the two above
  - published vol indices (GVZ/OVX/VIX)   -> tested separately below

So the spike checks the two things that matter:
  1) Can I get a live option chain WITH implied vol for SLV? (the hard part)
  2) Can I get the published vol indices (^GVZ gold, ^OVX crude, ^VIX)?
"""

import sys

SYMBOL = "SLV"


def spike_option_chain():
    import yfinance as yf
    import numpy as np

    print(f"\n=== 1. OPTION CHAIN + IMPLIED VOL for {SYMBOL} (yfinance) ===")
    tkr = yf.Ticker(SYMBOL)

    spot = None
    try:
        spot = tkr.fast_info["last_price"]
    except Exception:
        try:
            spot = tkr.history(period="1d")["Close"].iloc[-1]
        except Exception:
            pass
    print(f"spot price: {spot}")

    expirations = tkr.options
    if not expirations:
        print("!! NO expirations returned -> chain not available via yfinance")
        return False
    print(f"expirations available: {len(expirations)} "
          f"(first few: {expirations[:4]})")

    # Pick the nearest expiry and find the ATM-ish call.
    exp = expirations[0]
    chain = tkr.option_chain(exp)
    calls = chain.calls
    print(f"\nnearest expiry {exp}: {len(calls)} calls, {len(chain.puts)} puts")
    cols = [c for c in ["strike", "lastPrice", "bid", "ask",
                        "impliedVolatility", "volume", "openInterest"]
            if c in calls.columns]
    if "impliedVolatility" not in calls.columns:
        print("!! chain has NO impliedVolatility column")
        return False

    if spot is not None:
        calls = calls.assign(_dist=(calls["strike"] - spot).abs())
        atm = calls.sort_values("_dist").head(5)
    else:
        atm = calls.head(5)

    print("\nATM-ish calls (this is the live IV signal we'd store):")
    print(atm[cols].to_string(index=False))

    iv_vals = calls["impliedVolatility"].replace(0, np.nan).dropna()
    print(f"\nIV coverage: {len(iv_vals)}/{len(calls)} calls have a non-zero IV")
    if len(iv_vals):
        print(f"IV range on this expiry: "
              f"{iv_vals.min():.1%} .. {iv_vals.max():.1%}")
    return len(iv_vals) > 0


def spike_vol_indices():
    import yfinance as yf

    print("\n=== 2. PUBLISHED VOL INDICES (^VIX ^GVZ ^OVX) ===")
    ok = False
    for sym, label in [("^VIX", "equity VIX"),
                       ("^GVZ", "gold vol"),
                       ("^OVX", "crude oil vol")]:
        try:
            h = yf.Ticker(sym).history(period="5d")
            if len(h):
                last = h["Close"].iloc[-1]
                print(f"  {sym:6} ({label:13}) last close: {last:.2f}")
                ok = True
            else:
                print(f"  {sym:6} ({label:13}) -> no data")
        except Exception as e:
            print(f"  {sym:6} ({label:13}) -> ERROR {e}")
    return ok


def spike_realized_vol():
    import yfinance as yf
    import numpy as np

    print(f"\n=== 3. REALIZED VOL (for IV-RV spread) {SYMBOL} ===")
    h = yf.Ticker(SYMBOL).history(period="6mo")
    if len(h) < 30:
        print("!! not enough price history")
        return False
    rets = np.log(h["Close"]).diff().dropna()
    rv_30 = rets.tail(30).std() * np.sqrt(252)
    print(f"30-day realized vol (annualized): {rv_30:.1%}")
    print(f"price history rows available: {len(h)} (for backfill)")
    return True


if __name__ == "__main__":
    r1 = r2 = r3 = False
    try:
        r1 = spike_option_chain()
    except Exception as e:
        print(f"option-chain spike FAILED: {type(e).__name__}: {e}")
    try:
        r2 = spike_vol_indices()
    except Exception as e:
        print(f"vol-index spike FAILED: {type(e).__name__}: {e}")
    try:
        r3 = spike_realized_vol()
    except Exception as e:
        print(f"realized-vol spike FAILED: {type(e).__name__}: {e}")

    print("\n=== VERDICT ===")
    print(f"  option chain w/ IV : {'OK' if r1 else 'NO'}")
    print(f"  vol indices        : {'OK' if r2 else 'NO'}")
    print(f"  realized vol       : {'OK' if r3 else 'NO'}")
    sys.exit(0 if r1 else 1)