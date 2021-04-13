// SPDX-License-Identifier: Apache-2.0

/*
 * Copyright 2020, Offchain Labs, Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *    http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

pragma solidity ^0.6.11;

import "../libraries/aeERC20.sol";
import "arb-bridge-eth/contracts/libraries/Cloneable.sol";
import "./IArbToken.sol";
import "./ArbTokenBridge.sol";
import "../libraries/BytesParser.sol";

/**
 * @title Standard (i.e., non-custom) contract deployed by ArbTokenBridge.sol as L2 ERC20. Includes standard ERC20 interface plus additional methods for deposits/withdraws
 */
contract StandardArbERC20 is aeERC20, Cloneable, IArbToken {
    ArbTokenBridge public bridge;
    address public l1Address;

    modifier onlyBridge {
        require(msg.sender == address(bridge), "ONLY_BRIDGE");
        _;
    }

    function bridgeInit(address _l1Address, bytes memory _data) external override returns (bool) {
        require(address(l1Address) == address(0), "Already inited");
        bridge = ArbTokenBridge(msg.sender);
        l1Address = _l1Address;

        (bytes memory name, bytes memory symbol, bytes memory decimals) =
            abi.decode(_data, (bytes, bytes, bytes));
        // what if decode reverts? shouldn't as this is encoded by L1 contract

        aeERC20.initialize(
            BytesParserWithDefault.toString(name, ""),
            BytesParserWithDefault.toString(symbol, ""),
            BytesParserWithDefault.toUint8(decimals, 18)
        );
        return true;
    }

    /**
     * @notice Mint tokens on L2. Callable path is EthErc20Bridge.depositToken (which handles L1 escrow), which triggers ArbTokenBridge.mintFromL1, which calls this
     * @param account recipient of tokens
     * @param amount amount of tokens minted
     */
    function bridgeMint(address account, uint256 amount) external override onlyBridge {
        _mint(account, amount);
    }

    /**
     * @notice Initiates a token withdrawal
     * @param destination destination address
     * @param amount amount of tokens withdrawn
     */
    function withdraw(address destination, uint256 amount) external {
        _burn(msg.sender, amount);
        bridge.withdraw(l1Address, destination, amount);
    }

    /**
     * @notice Migrate tokens from to a custom token contract; this should only happen/matter if a standard ERC20 is deployed for an L1 custom contract before the L2 custom contract gets registered
     * @param destination destination address
     * @param amount amount of tokens withdrawn
     */
    function migrate(address destination, uint256 amount) external {
        _burn(msg.sender, amount);
        bridge.migrate(l1Address, destination, amount);
    }
}
