import os

proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
if proxy:
    try:
        import yfinance

        # curl_cffi 0.15 stores trust_env but never reads it, so the
        # HTTPS_PROXY environment alone does not move Yahoo requests.
        yfinance.config.network.proxy = proxy
    except Exception:
        pass
