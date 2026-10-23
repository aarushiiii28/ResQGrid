// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// Minimal anchor registry for the ResQGrid ledger (simulated demo). It stores nothing but a Merkle root and the
/// block time it was first anchored. No personal data, no logic, no admin: a root cannot be changed or removed.
contract ResQGridAnchor {
    event Anchored(bytes32 indexed root, address indexed by, uint256 at);

    /// root => block timestamp it was anchored at (0 = never)
    mapping(bytes32 => uint256) public anchoredAt;

    function anchor(bytes32 root) external {
        require(anchoredAt[root] == 0, "already anchored");
        anchoredAt[root] = block.timestamp;
        emit Anchored(root, msg.sender, block.timestamp);
    }
}
