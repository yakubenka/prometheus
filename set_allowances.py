"""
Polymarket — Set Allowances + Deposit (one-time setup)
"""
import os, sys

USDC_ADDRESS      = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
USDC_E_ADDRESS    = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS       = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
EXCHANGE_ADDRESS  = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_ADAPTER  = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
# Polymarket deposit contract
DEPOSIT_CONTRACT  = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

MAX_INT = 2**256 - 1

ERC20_ABI = [
    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
]

CTF_ABI = [
    {"inputs":[{"name":"operator","type":"address"},{"name":"approved","type":"bool"}],"name":"setApprovalForAll","outputs":[],"stateMutability":"nonpayable","type":"function"},
]

def send_tx(w3, contract_fn, addr, private_key):
    tx = contract_fn.build_transaction({
        "from": addr,
        "nonce": w3.eth.get_transaction_count(addr, "pending"),
        "gas": 150000,
        "gasPrice": int(w3.eth.gas_price * 1.2),
    })
    signed = w3.eth.account.sign_transaction(tx, private_key)
    txhash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(txhash, timeout=60)
    return txhash.hex()[:16], receipt.status

def main():
    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    public_key  = os.environ.get("POLYMARKET_FUNDER", "")
    if not private_key:
        print("ERROR: POLYMARKET_PRIVATE_KEY must be set")
        sys.exit(1)
    # POLYMARKET_FUNDER is optional for EOA mode
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    try:
        from web3 import Web3
        RPC_URLS = [
            "https://polygon-bor-rpc.publicnode.com",
            "https://rpc.ankr.com/polygon",
            "https://1rpc.io/matic",
        ]
        w3 = None
        for rpc in RPC_URLS:
            try:
                _w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
                if _w3.is_connected():
                    w3 = _w3
                    print(f"Connected to {rpc}")
                    break
            except Exception:
                continue
        if not w3:
            print("ERROR: Cannot connect to Polygon RPC")
            sys.exit(1)

        account = w3.eth.account.from_key(private_key)
        addr    = account.address
        print(f"Wallet: {addr}")

        pol = w3.eth.get_balance(addr)
        print(f"POL: {w3.from_wei(pol,'ether'):.4f}")

        spenders = [EXCHANGE_ADDRESS, NEG_RISK_ADAPTER, NEG_RISK_EXCHANGE]

        for usdc_addr, name in [(USDC_ADDRESS,"USDC"),(USDC_E_ADDRESS,"USDC.e")]:
            usdc = w3.eth.contract(address=Web3.to_checksum_address(usdc_addr), abi=ERC20_ABI)
            bal  = usdc.functions.balanceOf(addr).call()
            print(f"\n{name} balance: {bal/1e6:.2f}")
            for sp in spenders:
                sp_cs  = Web3.to_checksum_address(sp)
                cur    = usdc.functions.allowance(addr, sp_cs).call()
                if cur > 10**24:
                    print(f"  {name} -> {sp[:10]} already approved")
                    continue
                print(f"  Approving {name} -> {sp[:10]}...")
                try:
                    txh, status = send_tx(w3, usdc.functions.approve(sp_cs, MAX_INT), addr, private_key)
                    print(f"  Done: {txh} status={status}")
                except Exception as e:
                    print(f"  Error: {e}")

        ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
        for sp in spenders:
            sp_cs = Web3.to_checksum_address(sp)
            print(f"\nApproving CTF -> {sp[:10]}...")
            try:
                txh, status = send_tx(w3, ctf.functions.setApprovalForAll(sp_cs, True), addr, private_key)
                print(f"  Done: {txh} status={status}")
            except Exception as e:
                print(f"  Error: {e}")

        print("\n✅ ALL ALLOWANCES SET!")

        # Now check CLOB balance via py_clob_client
        print("\nChecking CLOB balance...")
        try:
            from py_clob_client.client import ClobClient
            client = ClobClient("https://clob.polymarket.com", key=private_key, chain_id=137, signature_type=0)
            client.set_api_creds(client.create_or_derive_api_creds())
            bal = client.get_balance()
            print(f"CLOB balance: {bal}")
        except Exception as e:
            print(f"CLOB check: {e}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
