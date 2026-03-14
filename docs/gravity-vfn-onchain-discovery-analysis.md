# Gravity VFN 链上发现机制分析

## 概述

本文档分析了 Gravity 中 VFN（Validator Full Node）通过链上信息连接 Validator 的机制，包括 Aptos 原版设计的限制、Gravity 的修改方案，以及相关的安全考虑。

---

## 1. 链上存储的 Validator 信息

### 1.1 Solidity 合约中的数据结构

Gravity 的系统合约（`Types.sol`）中存储了 Validator 的关键信息：

```solidity
struct ValidatorConsensusInfo {
    address validator;           // Validator 身份地址 (EVM 地址)
    bytes consensusPubkey;       // BLS12-381 共识公钥
    bytes consensusPop;          // BLS 密钥的所有权证明
    uint256 votingPower;         // 投票权重
    uint64 validatorIndex;       // 验证者索引
    bytes networkAddresses;      // ⭐ P2P 网络地址（Validator 网络）
    bytes fullnodeAddresses;     // ⭐ Fullnode 同步地址（VFN 网络）
}
```

### 1.2 networkAddresses 的格式

网络地址使用 **multiaddr** 格式，包含完整的连接信息：

```
/ip4/127.0.0.1/tcp/2024/noise-ik/2d86b40a1d692c0749a0a0426e2021ee24e2430da0f5bb9c2ae6c586bf3e0a0f/handshake/0
│      │          │        │                              │                                    │
│      │          │        │                              │                                    └── 协议版本
│      │          │        │                              └── x25519 公钥 (32 bytes = 64 hex)
│      │          │        └── Noise_IK 协议标识
│      │          └── TCP 端口
│      └── IPv4 地址
└── 协议类型
```

### 1.3 两种网络地址的用途

| 字段 | 用途 | 端口示例 |
|-----|------|---------|
| `networkAddresses` | Validator ↔ Validator 互联 | 2024 |
| `fullnodeAddresses` | VFN → Validator 连接 | 2025 |

---

## 2. PeerId 的派生机制

### 2.1 Aptos 原版设计

PeerId 是从 **x25519 公钥**派生的，而不是 ED25519：

```rust
// types/src/account_address.rs
pub fn from_identity_public_key(identity_public_key: x25519::PublicKey) -> AccountAddress {
    let mut array = [0u8; AccountAddress::LENGTH];
    let pubkey_slice = identity_public_key.as_slice();
    // 取 x25519 公钥的后 16 bytes 作为 PeerId
    array.copy_from_slice(&pubkey_slice[x25519::PUBLIC_KEY_SIZE - AccountAddress::LENGTH..]);
    AccountAddress::new(array)
}
```

派生过程：

```
x25519 公钥 (32 bytes):
┌────────────────────────────────────────────────────────────────┐
│  前16 bytes (丢弃)        │  后16 bytes = PeerId               │
│  2d86b40a1d692c07        │  49a0a0426e2021ee24e2430da0f5bb9c  │
└────────────────────────────────────────────────────────────────┘
```

### 2.2 Gravity 的修改带来的问题

Gravity 将 PeerId 改为从 **consensus key (BLS)** 派生，导致：

| 项目 | Aptos 原版 | Gravity 修改后 |
|-----|-----------|---------------|
| PeerId 来源 | x25519 公钥 | BLS consensus key |
| networkAddresses 中的公钥 | x25519 | x25519（未变） |
| 匹配关系 | ✅ PeerId = f(x25519) | ❌ PeerId ≠ f(x25519) |

这导致 Noise 握手时的身份验证失败（会议中讨论的问题）。

### 2.3 解决方案

从 `networkAddresses` 的 `noise-ik` 部分提取 x25519 公钥，用它派生 PeerId：

```rust
// 从 multiaddr 解析出 x25519 公钥，用于 PeerId 验证
fn verify_peer(network_addresses: &str) -> PeerId {
    let x25519_pubkey = parse_noise_ik_pubkey(network_addresses);
    from_identity_public_key(x25519_pubkey)
}
```

**优点**：零合约改动，只需修改 SDK 中的验证逻辑。

---

## 3. Aptos VFN 网络的多层限制

### 3.1 问题验证

**结论：Aptos 原版的 VFN 确实无法通过 on-chain 方式连接到 Validator。**

这是通过多层限制实现的刻意设计：

### 3.2 限制层次详解

```
┌─────────────────────────────────────────────────────────────────┐
│  第一层：DiscoveryMethod                                         │
│  ─────────────────────────────────────────────────────────────  │
│  VFN 网络默认 discovery_method: None                            │
│  → 根本不会去读取链上信息                                        │
├─────────────────────────────────────────────────────────────────┤
│  第二层：PeerRole 标记                                           │
│  ─────────────────────────────────────────────────────────────  │
│  extract_validator_set_updates() 函数会根据网络类型标记角色：    │
│  - Validator 网络: PeerRole::Validator                          │
│  - VFN/Public 网络: PeerRole::ValidatorFullNode                 │
├─────────────────────────────────────────────────────────────────┤
│  第三层：upstream_roles 限制                                     │
│  ─────────────────────────────────────────────────────────────  │
│  VFN 网络的 FullNode 只允许拨号 [Validator]                      │
│  ValidatorFullNode 不在允许列表中                                │
│  → 即使发现了也不会连接                                          │
└─────────────────────────────────────────────────────────────────┘
```

### 3.3 关键代码分析

**extract_validator_set_updates** - 角色标记：

```rust
pub(crate) fn extract_validator_set_updates(
    network_context: NetworkContext,
    node_set: ValidatorSet,
) -> PeerSet {
    let is_validator = network_context.network_id().is_validator_network();

    node_set.into_iter().map(|info| {
        let addrs = if is_validator {
            config.validator_network_addresses()   // Validator 网络
        } else {
            config.fullnode_network_addresses()    // VFN/Public 网络
        };

        let peer_role = if is_validator {
            PeerRole::Validator           // Validator 网络 → 标记为 Validator
        } else {
            PeerRole::ValidatorFullNode   // VFN 网络 → 标记为 ValidatorFullNode
        };
        (peer_id, Peer::from_addrs(peer_role, addrs))
    })
}
```

**upstream_roles** - 连接限制：

| NetworkId | 本节点角色 | 允许拨号的 PeerRole | 结果 |
|-----------|-----------|-------------------|------|
| Validator | - | `[Validator]` | ✅ Validator 互联 |
| Public | - | `[PreferredUpstream, Upstream, ValidatorFullNode]` | ✅ PFN 可连接 |
| Vfn | Validator | `[]` | Validator 不主动拨号 |
| Vfn | FullNode | `[Validator]` | ❌ 无法匹配 ValidatorFullNode |

### 3.4 为什么无法匹配？

```
VFN 网络的 FullNode 尝试通过 on-chain discovery 连接：

1. 读取链上 ValidatorSet
   └── Validator A, B, C...

2. extract_validator_set_updates 处理
   └── is_validator = false (因为是 VFN 网络)
   └── 全部标记为 PeerRole::ValidatorFullNode

3. choose_peers_to_dial 筛选
   └── upstream_roles = [Validator]
   └── 检查: ValidatorFullNode ∈ [Validator] ?
   └── ❌ 不包含！

4. 结果：不会拨号连接
```

---

## 4. Aptos 的设计意图

### 4.1 三层网络架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                          公开 (链上)                                 │
├─────────────────────────────────────────────────────────────────────┤
│  validator_network_addresses  →  Validator 互联                     │
│  fullnode_network_addresses   →  PFN 连接 (Public Network)          │
├─────────────────────────────────────────────────────────────────────┤
│                          私有 (不公开)                               │
├─────────────────────────────────────────────────────────────────────┤
│  VFN Network 端口             →  只给自己的 VFN 用，静态配置         │
└─────────────────────────────────────────────────────────────────────┘
```

### 4.2 设计理由

| 考量 | 说明 |
|-----|------|
| **安全隔离** | VFN 端口是 Validator 的"后门"，不应公开暴露 |
| **DDoS 防护** | 公开地址会成为攻击目标 |
| **运营控制** | Validator 运营者想控制谁能连接其 VFN |
| **架构分层** | VFN 是私有基础设施，不属于公开网络 |

### 4.3 Aptos 的 VFN 配置方式

```yaml
# validator_full_node.yaml
full_node_networks:
    # Public 网络 - 使用 on-chain discovery
    - network_id: "public"
      discovery_method: "onchain"

    # VFN 网络 - 使用静态 seeds
    - network_id:
          private: "vfn"
      seeds:                            # ← 静态配置，不是 on-chain
        00000000...1237:
          addresses:
          - "/ip4/127.0.0.1/tcp/6181/noise-ik/.../handshake/0"
          role: "Validator"
```

---

## 5. Gravity 的修改方案

### 5.1 需要突破的限制

| 层级 | Aptos 原版 | Gravity 修改 | PR |
|-----|-----------|-------------|-----|
| DiscoveryMethod | None | Onchain | 配置修改 |
| upstream_roles | `[Validator]` | `[Validator, ValidatorFullNode]` | [PR #34](https://github.com/Galxe/gravity-aptos/pull/34) |
| 节点角色 | Validator 不主动拨号 | 降级为 FullNode | [PR #425](https://github.com/Galxe/gravity-sdk/pull/425) |
| PeerId 验证 | x25519 派生 | 临时移除检查 | 待解决 |

### 5.2 修改后的效果

```
修改后的连接流程：

1. VFN 网络配置 DiscoveryMethod::Onchain
                    ↓
2. extract_validator_set_updates 读取 fullnode_network_addresses
                    ↓
3. 链上 Validator 被标记为 PeerRole::ValidatorFullNode
                    ↓
4. choose_peers_to_dial 检查 upstream_roles
   upstream_roles = [Validator, ValidatorFullNode]  ← 修改后
                    ↓
5. 检查: ValidatorFullNode ∈ [Validator, ValidatorFullNode] ?
                    ↓
6. ✅ 包含！可以拨号连接
```

---

## 6. ValidatorFullNode 的含义澄清

### 6.1 容易混淆的概念

| 术语 | 实际含义 |
|-----|---------|
| `ValidatorFullNode` | **Validator 节点开放的 FullNode 服务端点**（PeerRole 标签） |
| `fullnode_network_addresses` | **Validator 节点**上的 VFN 端口地址 |
| 普通 Full Node | 独立运行的全节点，**不在链上** |

### 6.2 链上只有 Validator 信息

```
链上 ValidatorSet:
┌─────────────────────────────────────────────────────────────┐
│  Validator A                                                 │
│  ├── validator_network_addresses: /ip4/.../tcp/6180/...     │
│  └── fullnode_network_addresses:  /ip4/.../tcp/6181/...     │
├─────────────────────────────────────────────────────────────┤
│  Validator B                                                 │
│  └── ...                                                     │
└─────────────────────────────────────────────────────────────┘

注意：普通 Full Node 的地址 ❌ 不在链上！
```

### 6.3 连接安全性

通过 on-chain discovery，Full Node 只会连接到 Validator，不会连接到其他 Full Node：

```
Full Node X 通过 on-chain discovery：

1. 读取链上 ValidatorSet
   └── 获取 Validator A, B, C 的 fullnode_addresses

2. 连接目标
   └── ✅ Validator A (在链上)
   └── ✅ Validator B (在链上)
   └── ✅ Validator C (在链上)
   └── ❌ Full Node Y (不在链上，无法发现)
```

---

## 7. 待解决问题

### 7.1 PeerId 验证问题

**问题**：Gravity 将 PeerId 从 x25519 派生改为从 BLS consensus key 派生，导致 Noise 握手验证失败。

**临时方案**：移除 PeerId 检查（有安全风险）

**推荐方案**：从 `networkAddresses` 的 `noise-ik` 部分提取 x25519 公钥，用它派生期望的 PeerId 进行验证。

```rust
// 解决方案：从 multiaddr 提取 x25519 公钥
fn extract_x25519_from_multiaddr(addr: &str) -> Option<x25519::PublicKey> {
    // 解析 /noise-ik/{pubkey}/ 部分
    // 返回 x25519 公钥
}

fn verify_peer_id(remote_peer_id: PeerId, network_address: &str) -> bool {
    let x25519_pubkey = extract_x25519_from_multiaddr(network_address)?;
    let expected_peer_id = from_identity_public_key(x25519_pubkey);
    remote_peer_id == expected_peer_id
}
```

### 7.2 安全考虑

| 风险 | 说明 | 缓解措施 |
|-----|------|---------|
| Validator VFN 端口暴露 | 任何人可尝试连接 | 链上验证、速率限制 |
| DDoS 攻击 | 恶意节点消耗资源 | 连接数限制、防火墙 |
| 身份伪造 | 恶意节点冒充 Validator | 恢复 PeerId 验证 |

---

## 8. 总结

### 8.1 Aptos 原版设计

- VFN 网络是 Validator 的私有基础设施
- 不支持通过链上发现自动连接
- 需要手动配置 seeds

### 8.2 Gravity 的修改

- 允许 VFN 通过链上发现连接 Validator
- 修改了 `upstream_roles` 限制
- 简化了运维（不需要手动配置 seeds）

### 8.3 链上信息足够性

**是的，链上信息足够支持 VFN 安全连接到 Validator**：

- `fullnodeAddresses` 包含完整的 multiaddr
- multiaddr 中的 `noise-ik` 部分包含 x25519 公钥
- 可以用于 Noise_IK 握手和身份验证

### 8.4 后续工作

1. 恢复 PeerId 验证（从 x25519 派生）
2. 添加连接速率限制
3. 完善网络监控和告警
