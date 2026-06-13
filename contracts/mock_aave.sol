// SPDX-License-Identifier: MIT

pragma solidity ^0.8.31;

interface IERC20 {
    function transfer(address to, uint256 value) external returns (bool);
    function transferFrom(address from, address to, uint256 value) external returns (bool);
    function mint(address to, uint256 amount) external;
    function burn(address from, uint256 amount) external;
}

contract MockAToken {
    mapping(address => uint256) public principalOf;
    mapping(address => uint256) public depositBlock;
    address public underlyingAsset;
    uint256 public constant PER_BLOCK_YIELD = 10; // 0.1% per block (10 bps)

    constructor(address _underlying) {
        underlyingAsset = _underlying;
    }

    function mint(address to, uint256 amount) external {
        principalOf[to] += amount;
        depositBlock[to] = block.number;
    }

    function burn(address from, uint256 amount) external {
        uint256 currentBalance = balanceOf(from);
        require(currentBalance >= amount, "Insufficient balance");
        uint256 principal = principalOf[from];
        if (principal >= amount) {
            principalOf[from] -= amount;
        } else {
            principalOf[from] = 0;
        }
        if (principalOf[from] == 0) {
            delete depositBlock[from];
        }
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        uint256 currentBalance = balanceOf(msg.sender);
        require(currentBalance >= amount, "Insufficient balance");
        principalOf[msg.sender] -= amount;
        principalOf[to] += amount;
        return true;
    }

    function balanceOf(address account) public view returns (uint256) {
        uint256 principal = principalOf[account];
        if (principal == 0) return 0;
        uint256 blocksElapsed = block.number - depositBlock[account];
        uint256 interest = principal * blocksElapsed * PER_BLOCK_YIELD / 10000;
        return principal + interest;
    }
}

contract MockAave {
    mapping(address => address) public aTokens;
    mapping(address => uint256) public deposits;

    event Supply(address indexed user, address indexed asset, uint256 amount);
    event Withdraw(address indexed user, address indexed asset, uint256 amount);

    function setAToken(address asset, address aToken) external {
        aTokens[asset] = aToken;
    }

    function getReserveAToken(address asset) external view returns (address) {
        return aTokens[asset];
    }

    function supply(
        address asset,
        uint256 amount,
        address onBehalfOf,
        uint16 referralCode
    ) external {
        IERC20 assetToken = IERC20(asset);
        require(
            assetToken.transferFrom(msg.sender, address(this), amount),
            "Supply transfer failed"
        );
        deposits[asset] += amount;

        address aToken = aTokens[asset];
        if (aToken != address(0)) {
            MockAToken(aToken).mint(onBehalfOf, amount);
        }

        emit Supply(onBehalfOf, asset, amount);
    }

    function withdraw(
        address asset,
        uint256 amount,
        address to
    ) external returns (uint256) {
        if (deposits[asset] >= amount) {
            deposits[asset] -= amount;
        }

        address aToken = aTokens[asset];
        if (aToken != address(0)) {
            MockAToken(aToken).burn(msg.sender, amount);
        }

        IERC20 assetToken = IERC20(asset);
        require(assetToken.transfer(to, amount), "Withdraw transfer failed");
        emit Withdraw(to, asset, amount);
        return amount;
    }
}
