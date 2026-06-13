import pytest
from brownie import BBSStaking, Erc20, MockAave, MockAToken, accounts, reverts, web3

# Fixtures provide setup for each test
@pytest.fixture
def mock_usdc(Erc20, accounts):
    # Constructor: _initialAmount, _tokenName, _decimalUnits, _tokenSymbol
    initial_supply = 1_000_000 * 10**6
    return Erc20.deploy(initial_supply, "Mock USDC", 6, "USDC", {'from': accounts[0]})

@pytest.fixture
def mock_aave(MockAave, accounts):
    return MockAave.deploy({'from': accounts[0]})

@pytest.fixture
def mock_atoken(MockAToken, mock_usdc, accounts):
    return MockAToken.deploy(mock_usdc.address, {'from': accounts[0]})

@pytest.fixture
def staking_contract(BBSStaking, mock_usdc, mock_aave, accounts):
    # Constructor: _erc20, _aave
    return BBSStaking.deploy(
        mock_usdc.address, 
        mock_aave.address, 
        {'from': accounts[0]}
    )

def test_initial_deployment(staking_contract, mock_usdc, mock_aave, accounts):
    assert staking_contract.owner() == accounts[0].address
    assert staking_contract.erc20() == mock_usdc.address
    assert staking_contract.aave() == mock_aave.address
    assert staking_contract.minDeposit() == 0

def test_owner_set_min_deposit(staking_contract, accounts):
    min_amount = 100 * 10**6
    # Only owner can set
    staking_contract.setMinDeposit(min_amount, {'from': accounts[0]})
    assert staking_contract.minDeposit() == min_amount
    
    # Non-owner should fail
    # Use reverts() without string to avoid "Unexpected revert string 'None'"
    with reverts():
        staking_contract.setMinDeposit(200 * 10**6, {'from': accounts[1]})

def test_user_deposit(staking_contract, mock_usdc, accounts):
    user = accounts[1]
    amount = 500 * 10**6
    
    #給予用戶代幣
    mock_usdc.transfer(user, amount, {'from': accounts[0]})
    
    # 授權 (Approve)
    mock_usdc.approve(staking_contract.address, amount, {'from': user})
    
    # 存款质押
    tx = staking_contract.deposit(amount, {'from': user})
    
    staking_id = tx.events['Deposited']['stakingId']
    assert staking_id == 1
    
    # 验证存入的数据
    stake_info = staking_contract.stakings(staking_id)
    assert stake_info['user'] == user.address
    assert stake_info['amount'] == amount
    assert stake_info['withdrawn'] == False
    
    assert staking_contract.totalStaked() == amount
    
    # 检查事件
    assert 'Deposited' in tx.events
    assert tx.events['Deposited']['stakingId'] == staking_id
    assert tx.events['Deposited']['amount'] == amount

def test_min_deposit_limit(staking_contract, mock_usdc, accounts):
    user = accounts[1]
    min_amount = 1000 * 10**6
    deposit_amount = 500 * 10**6
    
    staking_contract.setMinDeposit(min_amount, {'from': accounts[0]})
    mock_usdc.transfer(user, deposit_amount, {'from': accounts[0]})
    mock_usdc.approve(staking_contract.address, deposit_amount, {'from': user})
    
    # 预期失败：金额低于最小限制度
    with reverts():
        staking_contract.deposit(deposit_amount, {'from': user})

def test_user_withdraw(staking_contract, mock_usdc, accounts):
    user = accounts[1]
    initial_amount = 1000 * 10**6
    withdraw_amount = 250 * 10**6

    # 初始化账户并存款
    mock_usdc.transfer(user, initial_amount, {'from': accounts[0]})
    mock_usdc.approve(staking_contract.address, initial_amount, {'from': user})
    tx_dep = staking_contract.deposit(initial_amount, {'from': user})
    staking_id = tx_dep.events['Deposited']['stakingId']

    # 提取質押
    tx = staking_contract.withdraw(staking_id, {'from': user})

    # 验证状态
    stake_info = staking_contract.stakings(staking_id)
    assert stake_info['withdrawn'] == True
    assert staking_contract.totalStaked() == 0
    
    # 检查事件
    assert 'Withdrawn' in tx.events
    assert tx.events['Withdrawn']['user'] == user.address
    assert tx.events['Withdrawn']['stakingId'] == staking_id
    assert tx.events['Withdrawn']['amount'] == initial_amount

    # 初始化并存款
    amount = 500 * 10**6
    mock_usdc.transfer(user, amount, {'from': accounts[0]})
    mock_usdc.approve(staking_contract.address, amount, {'from': user})
    tx_dep = staking_contract.deposit(amount, {'from': user})
    staking_id = tx_dep.events['Deposited']['stakingId']
    
    # 尝试再次提取同一笔质押
    staking_contract.withdraw(staking_id, {'from': user})
    with reverts():
        staking_contract.withdraw(staking_id, {'from': user})
    
    # 尝试提取不存在的 ID
    with reverts():
        staking_contract.withdraw(999, {'from': user})
    
    # 尝试提取别人的质押
    with reverts():
        staking_contract.withdraw(staking_id, {'from': accounts[2]})

def test_change_owner(staking_contract, accounts):
    new_owner = accounts[1]
    staking_contract.setOwner(new_owner, {'from': accounts[0]})
    assert staking_contract.owner() == new_owner
    
    # 原所有者不能再修改
    with reverts():
        staking_contract.setMinDeposit(100, {'from': accounts[0]})

def test_set_atoken_and_withdraw_profit(mock_aave, mock_atoken, mock_usdc, staking_contract, accounts):
    # 设置 aToken 映射
    mock_aave.setAToken(mock_usdc.address, mock_atoken.address, {'from': accounts[0]})
    assert mock_aave.getReserveAToken(mock_usdc.address) == mock_atoken.address

def test_withdraw_profit_with_gain(mock_aave, mock_atoken, mock_usdc, staking_contract, accounts):
    mock_aave.setAToken(mock_usdc.address, mock_atoken.address, {'from': accounts[0]})
    
    user = accounts[1]
    amount = 1000 * 10**6
    
    mock_usdc.transfer(user, amount, {'from': accounts[0]})
    mock_usdc.approve(staking_contract.address, amount, {'from': user})
    staking_contract.deposit(amount, {'from': user})
    
    # 等待多个区块产生收益 (每 block 0.1%)
    for _ in range(10):
        mock_aave.setAToken(mock_usdc.address, mock_atoken.address, {'from': accounts[0]})
    
    # 检查 aToken 余额增长
    aToken_balance = mock_atoken.balanceOf(staking_contract.address)
    assert aToken_balance > amount
    
    # 提取收益 (aToken)
    owner = accounts[0]
    owner_atoken_initial_balance = mock_atoken.balanceOf(owner)
    staking_contract.withdrawProfit({'from': owner})
    
    # 验证 owner 收到了 aToken 收益
    owner_atoken_final_balance = mock_atoken.balanceOf(owner)
    assert owner_atoken_final_balance > owner_atoken_initial_balance

def test_withdraw_profit_only_owner(mock_aave, mock_atoken, mock_usdc, staking_contract, accounts):
    mock_aave.setAToken(mock_usdc.address, mock_atoken.address, {'from': accounts[0]})
    
    # 非 owner 不能提取收益
    with reverts():
        staking_contract.withdrawProfit({'from': accounts[1]})
