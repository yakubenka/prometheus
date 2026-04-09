"""
Тест баланса Polymarket CLOB - v2
"""
import os, requests

def main():
    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    funder      = os.environ.get("POLYMARKET_FUNDER", "")

    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    print(f"Funder: {funder}")

    from py_clob_client.client import ClobClient

    # Проверяем все доступные методы
    c = ClobClient("https://clob.polymarket.com", key=private_key, chain_id=137,
                   signature_type=0, funder=funder)
    c.set_api_creds(c.create_or_derive_api_creds())

    methods = [m for m in dir(c) if 'balance' in m.lower() or 'allowance' in m.lower()]
    print(f"\nДоступные методы: {methods}")

    # Пробуем get_balance_allowance
    for method_name in methods:
        print(f"\n--- {method_name} ---")
        try:
            method = getattr(c, method_name)
            result = method()
            print(f"Result: {result}")
        except Exception as e:
            print(f"Error: {e}")

    # Прямой HTTP запрос
    print("\n--- Direct HTTP /balance-allowance ---")
    try:
        headers = c.get_headers("GET", "/balance-allowance")
        r = requests.get("https://clob.polymarket.com/balance-allowance",
                        params={"asset_type": "USDC", "signature_type": "0"},
                        headers=headers, timeout=10)
        print(f"Status: {r.status_code}")
        print(f"Response: {r.text[:500]}")
    except Exception as e:
        print(f"Error: {e}")

    print("\nDone.")

if __name__ == "__main__":
    main()
