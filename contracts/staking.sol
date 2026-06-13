// SPDX-License-Identifier: MIT

pragma solidity ^0.8.31;

interface IERC20 {
    function transferFrom(
        address from,
        address to,
        uint256 value
    ) external returns (bool);

    function transfer(address to, uint256 value) external returns (bool);

    function approve(address spender, uint256 amount) external returns (bool);

    function balanceOf(address account) external view returns (uint256);
}

interface IAave {
    function supply(
        address asset,
        uint256 amount,
        address onBehalfOf,
        uint16 referralCode
    ) external;

    function withdraw(
        address asset,
        uint256 amount,
        address to
    ) external returns (uint256);

    function getReserveAToken(address asset) external view returns (address);
}

// Renamed from Transfer to Staking
struct Staking {
    address user;
    uint256 amount;
    uint256 timestamp;
    bool withdrawn;
}

contract BBSStaking {
    address public erc20;
    address public aave;
    address public owner;
    
    uint256 public minDeposit = 0;
    uint256 public totalStaked = 0;
    uint256 public stakingCount = 0;

    mapping(uint256 => Staking) public stakings;

    event Deposited(address indexed user, uint256 stakingId, uint256 amount);
    event Withdrawn(address indexed user, uint256 stakingId, uint256 amount);
    event MinDepositUpdated(uint256 newMinDeposit);
    event OwnerChanged(address indexed oldOwner, address indexed newOwner);
    event WithdrawProfit(address indexed owner, uint256 profit);


    constructor(address _erc20, address _aave) {
        erc20 = _erc20;
        aave = _aave;
        owner = msg.sender;
    }

    function deposit(uint256 amount) external {
        require(amount >= minDeposit, "Amount below minimum deposit");
        require(amount > 0, "Amount must be > 0");

        require(
            IERC20(erc20).transferFrom(msg.sender, address(this), amount),
            "Transfer from user failed"
        );

        // Supply to Aave to earn interest
        IERC20(erc20).approve(aave, amount);
        IAave(aave).supply(erc20, amount, address(this), 0);

        totalStaked += amount;

        stakingCount++;
        stakings[stakingCount] = Staking({
            user: msg.sender,
            amount: amount,
            timestamp: block.timestamp,
            withdrawn: false
        });

        emit Deposited(msg.sender, stakingCount, amount);
    }

    function withdraw(uint256 stakingId) external {
        Staking storage s = stakings[stakingId];
        require(s.user == msg.sender, "Not your staking");
        require(!s.withdrawn, "Already withdrawn");
        uint256 amount = s.amount;
        require(amount > 0, "Amount must be > 0");

        s.withdrawn = true;
        totalStaked -= amount;

        // Withdraw from Aave
        IAave(aave).withdraw(erc20, amount, address(this));
        
        require(
            IERC20(erc20).transfer(msg.sender, amount),
            "Transfer to user failed"
        );

        emit Withdrawn(msg.sender, stakingId, amount);
    }

    function setOwner(address _newOwner) external {
        require(msg.sender == owner, "Only owner");
        emit OwnerChanged(owner, _newOwner);
        owner = _newOwner;
    }

    function setMinDeposit(uint256 _amount) external {
        require(msg.sender == owner, "Only owner");
        minDeposit = _amount;
        emit MinDepositUpdated(_amount);
    }

    function withdrawProfit() external {
        require(msg.sender == owner, "Only owner");
        address aTokenAddress = IAave(aave).getReserveAToken(erc20);
        uint256 aaveBalance = IERC20(aTokenAddress).balanceOf(address(this));
        uint256 profit = aaveBalance > totalStaked ? aaveBalance - totalStaked : 0;
        require(profit > 0, "No profit to withdraw");
        require(IERC20(aTokenAddress).transfer(owner, profit), "Transfer failed");
        emit WithdrawProfit(owner, profit);
    }
}
