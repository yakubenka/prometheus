"""
Polymarket — Set Allowances via web3 (one-time setup)
Устанавливает approve для USDC и CTF контрактов Polymarket.
Запускается один раз для EOA/MetaMask кошелька.
"""
import os
import sys

# Polymarket контракты на Polygon
USDC_ADDRESS         = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # Native USDC
USDC_E_ADDRESS       = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e (bridged)
CTF_ADDRESS          = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
EXCHANGE_ADDRESS     = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_ADAPTER     = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
NEG_RISK_EXCHANGE    = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

MAX_INT = 2**256 - 1

ERC20_ABI = [
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount",  "type": "uint256"}
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "owner",   "type": "address"},
            {"name": "spender", "type": "address"}
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]

CTF_ABI = [
    {
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"}
        ],
        "name": "setApprovalForAll",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]

def main():
    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    public_key  = os.environ.get("POLYMARKET_FUNDER", "")

    if not private_key or not public_key:
        print("ERROR: POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER must be set")
        sys.exit(1)

    # Add 0x prefix if missing
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    print(f"Setting allowances for: {public_key[:12]}...")

    try:
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
        account = w3.eth.account.from_key(private_key)
        addr    = account.address
        print(f"Wallet: {addr}")

        # Check POL balance for gas
        pol_balance = w3.eth.get_balance(addr)
        print(f"POL balance: {w3.from_wei(pol_balance, 'ether'):.4f}")
        if pol_balance < w3.to_wei(0.01, 'ether'):
            print("WARNING: Low POL balance — may not have enough for gas!")

        spenders = [EXCHANGE_ADDRESS, NEG_RISK_ADAPTER, NEG_RISK_EXCHANGE]

        # Approve USDC and USDC.e for all spenders
        for usdc_addr, name in [(USDC_ADDRESS, "USDC"), (USDC_E_ADDRESS, "USDC.e")]:
            usdc = w3.eth.contract(address=Web3.to_checksum_address(usdc_addr), abi=ERC20_ABI)
            balance = usdc.functions.balanceOf(addr).call()
            print(f"\n{name} balance: {balance / 1e6:.2f}")

            for spender in spenders:
                spender_cs = Web3.to_checksum_address(spender)
                current = usdc.functions.allowance(addr, spender_cs).call()
                if current > 10**24:
                    print(f"  {name} → {spender[:10]}... already approved")
                    continue
                print(f"  Approving {name} → {spender[:10]}...")
                tx = usdc.functions.approve(spender_cs, MAX_INT).build_transaction({
                    "from":  addr,
                    "nonce": w3.eth.get_transaction_count(addr),
                    "gas":   100000,
                    "gasPrice": w3.eth.gas_price,
                })
                signed = w3.eth.account.sign_transaction(tx, private_key)
                txhash = w3.eth.send_raw_transaction(signed.raw_transaction)
                receipt = w3.eth.wait_for_transaction_receipt(txhash, timeout=60)
                print(f"  ✓ Done: {txhash.hex()[:16]}... status={receipt.status}")

        # Approve CTF for all spenders
        ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
        for spender in spenders:
            spender_cs = Web3.to_checksum_address(spender)
            print(f"\n  Approving CTF → {spender[:10]}...")
            tx = ctf.functions.setApprovalForAll(spender_cs, True).build_transaction({
                "from":  addr,
                "nonce": w3.eth.get_transaction_count(addr),
                "gas":   100000,
                "gasPrice": w3.eth.gas_price,
            })
            signed = w3.eth.account.sign_transaction(tx, private_key)
            txhash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(txhash, timeout=60)
            print(f"  ✓ Done: {txhash.hex()[:16]}... status={receipt.status}")

        print("\n✅ ALL ALLOWANCES SET — you can now trade!")

    except ImportError:
        print("ERROR: web3 not installed. Add 'web3' to requirements.txt")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
