from brownie import accounts, Erc20, BBSStaking

def main():
    # ==========================================
    # 1. 请在下方填入您的合约地址 (部署后的地址)
    # ==========================================
    MOCK_USDC_ADDRESS = "0x5FbDB2315678afecb367f032d93F642f64180aa3"        # 这里填 Mock USDC 的地址
    STAKING_CONTRACT_ADDRESS = "0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0" # 这里填 BBSStaking 合约的地址
    # ==========================================

    if not MOCK_USDC_ADDRESS or not STAKING_CONTRACT_ADDRESS:
        print("\n[错误] 请先编辑脚本，在 MOCK_USDC_ADDRESS 和 STAKING_CONTRACT_ADDRESS 中填入真实的地址！")
        return

    # 获取账户 (Anvil/Ganache 默认使用第一个账户)
    # 如果是在测试网/主网，请改用 accounts.load('your_account_id')
    user = accounts[0]
    print(f"\n[使用账户]: {user.address}")

    # 加载已部署的合约对象
    try:
        usdc = Erc20.at(MOCK_USDC_ADDRESS)
        staking = BBSStaking.at(STAKING_CONTRACT_ADDRESS)
    except Exception as e:
        print(f"[错误] 加载合约失败，请确认地址是否正确: {e}")
        return

    # ---------------------------------------------------------
    # 第一步: 从 Mock USDC 合约中 Mint (铸造) 一些代币到自己钱包
    # ---------------------------------------------------------
    # 注意：我已经在您的 mock_erc20.sol 中添加了 mint 函数
    mint_amount = 1000 * 10**6  # 铸造 1000 USDC (6位精度)
    print(f"\n[1/3] 正在铸造 {mint_amount/1e6} Mock USDC 到您的钱包...")
    usdc.mint(user, mint_amount, {'from': user})

    # ---------------------------------------------------------
    # 第二步: 授权 (Approve) 质押合约可以扣除您的 USDC
    # ---------------------------------------------------------
    stake_amount = 500 * 10**6  # 准备质押 500 USDC
    print(f"[2/3] 正在授权 {stake_amount/1e6} USDC 给质押合约...")
    usdc.approve(staking.address, stake_amount, {'from': user})

    # ---------------------------------------------------------
    # 第三步: 执行质押 (Staking/Deposit)
    # ---------------------------------------------------------
    print(f"[3/3] 正在向合约质押 {stake_amount/1e6} USDC...")
    tx = staking.deposit(stake_amount, {'from': user})

    # ---------------------------------------------------------
    # 结果验证
    # ---------------------------------------------------------
    print("\n==========================================")
    print("✨ 操作成功 ✨")
    print(f"当前账户余额: {usdc.balanceOf(user)/1e6} USDC")
    print(f"当前质押余额: {staking.stakedBalances(user)/1e6} USDC")
    print("==========================================\n")

if __name__ == "__main__":
    main()
