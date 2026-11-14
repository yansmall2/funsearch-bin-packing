"""No key required: compare default proxy route and direct TLS to DeepSeek.

Only GET /models is used. No completion request, proxy-setting mutation, or
certificate-verification bypass occurs. 401 means TLS and HTTP succeeded.
"""
import json
import ssl
import urllib.error
import urllib.request


def main():
    results = []
    for route, opener in (("default", urllib.request.build_opener()),
                          ("direct_diagnostic_only", urllib.request.build_opener(urllib.request.ProxyHandler({})))):
        try:
            with opener.open("https://api.deepseek.com/models", timeout=12) as response:
                results.append({"route": route, "http_status": response.status, "tls_connected": True})
        except urllib.error.HTTPError as error:
            results.append({"route": route, "http_status": error.code, "tls_connected": True})
        except Exception as error:
            results.append({"route": route, "tls_connected": False, "error_type": type(error).__name__,
                            "message": str(error)[:300]})
    print(json.dumps({"no_credentials_sent": True, "generation_requests": 0,
                      "openssl": ssl.OPENSSL_VERSION, "results": results}, indent=2))


if __name__ == "__main__":
    main()
