# Gravity 网络代码优化建议

本文档基于对 Gravity SDK 网络代码的分析，提出在保持设计意图不变的前提下的优化建议。

## 1. 当前代码问题概览

### 1.1 已标记的 TODO/FIXME

| 文件 | 行号 | 问题描述 |
|------|------|---------|
| `consensus_api.rs` | 164 | FIXME: VFN 角色处理是临时方案 |
| `network.rs` | 57 | TODO: 压缩等配置应可配置 |
| `network.rs` | 87 | TODO: 为什么 mempool 用 KLAST 而不是 FIFO |
| `bootstrap.rs` | 218 | TODO: 只有 validator 应订阅 reconf 事件 |
| `epoch_manager.rs` | 429 | TODO: 优化此处逻辑 |
| `epoch_manager.rs` | 1806 | TODO: 检查 self 是否在 available peers 中 |
| `round_manager.rs` | 320 | TODO: 评估 BlockRetriever 创建是否需要缓存 |

### 1.2 代码质量问题

- 大量 `unwrap()` 和 `expect()` 调用，缺少优雅的错误处理
- 硬编码的配置值
- 缺少细粒度的网络指标

## 2. 架构层面优化

### 2.1 VFN 角色处理正式化

**当前问题：**

```rust
// consensus_api.rs:164-166
if network_id.is_vfn_network() {
    // FIXME(nekomoto): This is a temporary solution to support block sync for
    // validator node which is not the current epoch validator.
    RoleType::FullNode
}
```

**优化方案：引入明确的验证者状态机**

```rust
/// 验证者状态枚举
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ValidatorState {
    /// 当前 epoch 活跃验证者，参与共识
    Active,
    /// 等待进入下一 epoch，需要同步区块
    Pending,
    /// 正在同步中（使用 VFN 网络）
    Syncing,
    /// 已退出验证者集合
    Retired,
}

impl ValidatorState {
    /// 根据状态决定网络角色
    pub fn network_role(&self) -> RoleType {
        match self {
            Self::Active => RoleType::Validator,
            Self::Pending | Self::Syncing | Self::Retired => RoleType::FullNode,
        }
    }

    /// 是否应该使用 VFN 网络
    pub fn should_use_vfn_network(&self) -> bool {
        matches!(self, Self::Pending | Self::Syncing)
    }

    /// 是否可以参与共识投票
    pub fn can_vote(&self) -> bool {
        matches!(self, Self::Active)
    }
}

/// 验证者状态管理器
pub struct ValidatorStateManager {
    current_state: ValidatorState,
    epoch: u64,
    account_address: AccountAddress,
}

impl ValidatorStateManager {
    pub fn new(account_address: AccountAddress) -> Self {
        Self {
            current_state: ValidatorState::Syncing,
            epoch: 0,
            account_address,
        }
    }

    /// 检查并更新状态
    pub fn update_state(&mut self, epoch_state: &EpochState) {
        let is_validator = epoch_state
            .verifier
            .get_ordered_account_addresses_iter()
            .any(|addr| addr == self.account_address);

        self.current_state = if is_validator {
            ValidatorState::Active
        } else if self.current_state == ValidatorState::Active {
            // 刚从活跃变为非活跃
            ValidatorState::Retired
        } else {
            ValidatorState::Pending
        };

        self.epoch = epoch_state.epoch;
    }

    pub fn state(&self) -> ValidatorState {
        self.current_state
    }
}
```

**使用示例：**

```rust
// consensus_api.rs 中的改进
let validator_state_manager = ValidatorStateManager::new(my_address);

let mut network_builder = NetworkBuilder::create(
    chain_id,
    validator_state_manager.state().network_role(),
    &network_config,
    // ...
);
```

### 2.2 统一 Peer 发现机制

**当前问题：**
- Validator 使用文件发现
- VFN 使用链上发现
- 配置方式不一致，维护成本高

**优化方案：支持混合发现模式**

```yaml
# validator.yaml - 新配置格式
discovery:
  # 主要发现方式
  primary:
    type: file
    path: "/tmp/gravity_node/discovery"
    refresh_interval_secs: 3600

  # 备选发现方式（主要方式失败时使用）
  fallback:
    type: onchain
    refresh_interval_secs: 300

  # 静态种子节点（始终尝试连接）
  static_seeds:
    - peer_id: "0x1234..."
      address: "/ip4/10.0.0.1/tcp/6180"
      role: Validator
```

```rust
/// 发现配置
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct DiscoveryConfig {
    pub primary: DiscoveryMethod,
    pub fallback: Option<DiscoveryMethod>,
    pub static_seeds: Vec<SeedPeer>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum DiscoveryMethod {
    File {
        path: PathBuf,
        refresh_interval_secs: u64,
    },
    Onchain {
        refresh_interval_secs: u64,
    },
    None,
}

/// 统一的发现服务
pub struct UnifiedDiscoveryService {
    config: DiscoveryConfig,
    peers: Arc<RwLock<HashMap<PeerId, PeerInfo>>>,
}

impl UnifiedDiscoveryService {
    pub async fn discover_peers(&self) -> Result<Vec<PeerInfo>> {
        // 1. 首先尝试主要发现方式
        match self.discover_with_method(&self.config.primary).await {
            Ok(peers) if !peers.is_empty() => return Ok(peers),
            Err(e) => {
                warn!("Primary discovery failed: {}", e);
            }
            _ => {}
        }

        // 2. 尝试备选方式
        if let Some(fallback) = &self.config.fallback {
            match self.discover_with_method(fallback).await {
                Ok(peers) => return Ok(peers),
                Err(e) => {
                    warn!("Fallback discovery failed: {}", e);
                }
            }
        }

        // 3. 返回静态种子节点
        Ok(self.config.static_seeds.iter().map(|s| s.into()).collect())
    }
}
```

## 3. 代码质量优化

### 3.1 移除 unwrap/expect，改用 Result

**当前问题：** `bootstrap.rs` 中的典型代码

```rust
// 问题代码
let my_addr = node_config.validator_network.as_ref().unwrap().peer_id();
let reconfig_events = event_subscription_service
    .subscribe_to_reconfigurations()
    .expect("Consensus must subscribe to reconfigurations");
```

**优化方案：**

```rust
/// 网络初始化错误类型
#[derive(Debug, thiserror::Error)]
pub enum NetworkInitError {
    #[error("Validator network not configured")]
    ValidatorNetworkNotConfigured,

    #[error("Failed to subscribe to reconfigurations: {0}")]
    SubscriptionFailed(String),

    #[error("Identity file not found: {path}")]
    IdentityNotFound { path: PathBuf },

    #[error("Invalid configuration: {0}")]
    InvalidConfig(String),
}

/// 改进后的代码
pub fn get_validator_peer_id(node_config: &NodeConfig) -> Result<PeerId, NetworkInitError> {
    node_config
        .validator_network
        .as_ref()
        .ok_or(NetworkInitError::ValidatorNetworkNotConfigured)?
        .peer_id()
        .map_err(|e| NetworkInitError::InvalidConfig(e.to_string()))
}

pub fn subscribe_to_reconfigurations(
    service: &mut EventSubscriptionService,
) -> Result<ReconfigNotificationListener, NetworkInitError> {
    service
        .subscribe_to_reconfigurations()
        .map_err(|e| NetworkInitError::SubscriptionFailed(e.to_string()))
}
```

### 3.2 配置可配置化

**当前问题：** `network.rs:57` 硬编码配置

```rust
/// TODO: make this configurable (e.g., for compression)
pub fn consensus_network_configuration(node_config: &NodeConfig) -> NetworkApplicationConfig {
    // 硬编码的配置
    let direct_send_protocols = aptos_consensus::network_interface::DIRECT_SEND.into();
    // ...
}
```

**优化方案：**

```yaml
# validator.yaml - 网络配置部分
network:
  consensus:
    compression_enabled: true
    compression_algorithm: "lz4"  # lz4, zstd, none
    max_channel_size: 1024
    queue_style: "FIFO"
    max_parallel_deserialization_tasks: 8

  mempool:
    max_channel_size: 1024
    queue_style: "KLAST"

  common:
    max_frame_size: 4194304  # 4 MiB
    max_message_size: 67108864  # 64 MiB
    ping_interval_ms: 10000
    ping_timeout_ms: 20000
```

```rust
/// 网络协议配置
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ProtocolConfig {
    pub compression_enabled: bool,
    pub compression_algorithm: CompressionAlgorithm,
    pub max_channel_size: usize,
    pub queue_style: QueueStyleConfig,
    pub max_parallel_deserialization_tasks: Option<usize>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum QueueStyleConfig {
    Fifo,
    Klast,
    Lifo,
}

impl From<QueueStyleConfig> for QueueStyle {
    fn from(config: QueueStyleConfig) -> Self {
        match config {
            QueueStyleConfig::Fifo => QueueStyle::FIFO,
            QueueStyleConfig::Klast => QueueStyle::KLAST,
            QueueStyleConfig::Lifo => QueueStyle::LIFO,
        }
    }
}

/// 改进后的配置函数
pub fn consensus_network_configuration(node_config: &NodeConfig) -> NetworkApplicationConfig {
    let config = &node_config.network.consensus;

    let network_service_config = NetworkServiceConfig::new(
        direct_send_protocols,
        rpc_protocols,
        aptos_channel::Config::new(config.max_channel_size)
            .queue_style(config.queue_style.clone().into())
            .counters(&PENDING_CONSENSUS_NETWORK_EVENTS),
    );

    // 如果启用压缩，包装协议
    if config.compression_enabled {
        network_service_config.with_compression(config.compression_algorithm)
    } else {
        network_service_config
    }
}
```

## 4. 区块同步优化

### 4.1 智能 Peer 选择

**当前问题：** `round_manager.rs:320`
- 每次创建 BlockRetriever 都重新构建 peer 列表
- 没有考虑 peer 的历史表现（延迟、成功率）

**优化方案：**

```rust
/// Peer 评分器
pub struct PeerScorer {
    /// 延迟统计 (peer_id -> 平均延迟 ms)
    latency_scores: HashMap<PeerId, ExponentialMovingAverage>,
    /// 成功率统计 (peer_id -> 成功率)
    success_rates: HashMap<PeerId, SuccessRateTracker>,
    /// 最后更新时间
    last_updated: Instant,
    /// 配置
    config: PeerScorerConfig,
}

#[derive(Debug, Clone)]
pub struct PeerScorerConfig {
    /// EMA 衰减因子
    pub ema_alpha: f64,
    /// 成功率窗口大小
    pub success_window_size: usize,
    /// 新 peer 的默认分数
    pub default_score: f64,
}

impl Default for PeerScorerConfig {
    fn default() -> Self {
        Self {
            ema_alpha: 0.3,
            success_window_size: 100,
            default_score: 0.5,
        }
    }
}

impl PeerScorer {
    pub fn new(config: PeerScorerConfig) -> Self {
        Self {
            latency_scores: HashMap::new(),
            success_rates: HashMap::new(),
            last_updated: Instant::now(),
            config,
        }
    }

    /// 记录请求结果
    pub fn record_request(&mut self, peer: PeerId, latency_ms: Option<u64>, success: bool) {
        // 更新延迟
        if let Some(latency) = latency_ms {
            self.latency_scores
                .entry(peer)
                .or_insert_with(|| ExponentialMovingAverage::new(self.config.ema_alpha))
                .update(latency as f64);
        }

        // 更新成功率
        self.success_rates
            .entry(peer)
            .or_insert_with(|| SuccessRateTracker::new(self.config.success_window_size))
            .record(success);

        self.last_updated = Instant::now();
    }

    /// 计算 peer 分数
    pub fn score_peer(&self, peer: &PeerId) -> f64 {
        let latency = self.latency_scores
            .get(peer)
            .map(|ema| ema.value())
            .unwrap_or(100.0);  // 默认 100ms

        let success_rate = self.success_rates
            .get(peer)
            .map(|tracker| tracker.rate())
            .unwrap_or(self.config.default_score);

        // 分数 = 成功率 / 归一化延迟
        // 延迟越低、成功率越高，分数越高
        let normalized_latency = (latency / 100.0).max(0.1);  // 避免除零
        success_rate / normalized_latency
    }

    /// 选择最佳 peers
    pub fn select_best_peers(&self, peers: &[PeerId], count: usize) -> Vec<PeerId> {
        let mut scored: Vec<_> = peers
            .iter()
            .map(|p| (*p, self.score_peer(p)))
            .collect();

        // 按分数降序排序
        scored.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));

        scored.into_iter().take(count).map(|(p, _)| p).collect()
    }

    /// 选择 peers，带随机探索
    pub fn select_peers_with_exploration(
        &self,
        peers: &[PeerId],
        count: usize,
        exploration_rate: f64,
    ) -> Vec<PeerId> {
        let mut rng = rand::thread_rng();
        let explore_count = (count as f64 * exploration_rate).ceil() as usize;
        let exploit_count = count - explore_count;

        let mut selected = self.select_best_peers(peers, exploit_count);

        // 随机选择一些 peer 用于探索
        let remaining: Vec<_> = peers
            .iter()
            .filter(|p| !selected.contains(p))
            .cloned()
            .collect();

        for _ in 0..explore_count {
            if remaining.is_empty() {
                break;
            }
            let idx = rng.gen_range(0..remaining.len());
            selected.push(remaining[idx]);
        }

        selected
    }
}

/// 指数移动平均
struct ExponentialMovingAverage {
    alpha: f64,
    value: f64,
    initialized: bool,
}

impl ExponentialMovingAverage {
    fn new(alpha: f64) -> Self {
        Self { alpha, value: 0.0, initialized: false }
    }

    fn update(&mut self, new_value: f64) {
        if self.initialized {
            self.value = self.alpha * new_value + (1.0 - self.alpha) * self.value;
        } else {
            self.value = new_value;
            self.initialized = true;
        }
    }

    fn value(&self) -> f64 {
        self.value
    }
}

/// 成功率追踪器
struct SuccessRateTracker {
    window: VecDeque<bool>,
    window_size: usize,
}

impl SuccessRateTracker {
    fn new(window_size: usize) -> Self {
        Self {
            window: VecDeque::with_capacity(window_size),
            window_size,
        }
    }

    fn record(&mut self, success: bool) {
        if self.window.len() >= self.window_size {
            self.window.pop_front();
        }
        self.window.push_back(success);
    }

    fn rate(&self) -> f64 {
        if self.window.is_empty() {
            return 0.5;
        }
        let successes = self.window.iter().filter(|&&s| s).count();
        successes as f64 / self.window.len() as f64
    }
}
```

### 4.2 并行区块获取

**当前问题：** 区块检索串行进行，效率低

**优化方案：**

```rust
/// 并行区块检索器
pub struct ParallelBlockRetriever {
    network: Arc<NetworkSender>,
    peer_scorer: Arc<RwLock<PeerScorer>>,
    config: ParallelRetrievalConfig,
}

#[derive(Debug, Clone)]
pub struct ParallelRetrievalConfig {
    /// 最大并行请求数
    pub max_parallel_requests: usize,
    /// 每个请求的最大区块数
    pub max_blocks_per_request: u64,
    /// 单个请求超时
    pub request_timeout: Duration,
    /// 整体操作超时
    pub overall_timeout: Duration,
}

impl Default for ParallelRetrievalConfig {
    fn default() -> Self {
        Self {
            max_parallel_requests: 4,
            max_blocks_per_request: 100,
            request_timeout: Duration::from_secs(5),
            overall_timeout: Duration::from_secs(30),
        }
    }
}

impl ParallelBlockRetriever {
    pub async fn retrieve_blocks(
        &self,
        start_block_id: HashValue,
        num_blocks: u64,
        target_block_id: HashValue,
        available_peers: Vec<PeerId>,
    ) -> Result<Vec<Block>, BlockRetrievalError> {
        if available_peers.is_empty() {
            return Err(BlockRetrievalError::NoPeersAvailable);
        }

        // 选择最佳 peers
        let peers = self.peer_scorer
            .read()
            .await
            .select_peers_with_exploration(
                &available_peers,
                self.config.max_parallel_requests,
                0.2,  // 20% 探索率
            );

        // 将请求分片
        let chunks = self.create_request_chunks(start_block_id, num_blocks, target_block_id);

        // 创建请求任务
        let mut futures = FuturesUnordered::new();
        let mut chunk_iter = chunks.into_iter();
        let mut peer_iter = peers.iter().cycle();

        // 初始化并行请求
        for _ in 0..self.config.max_parallel_requests.min(chunks.len()) {
            if let Some(chunk) = chunk_iter.next() {
                let peer = *peer_iter.next().unwrap();
                futures.push(self.fetch_chunk(peer, chunk));
            }
        }

        // 收集结果
        let mut results = Vec::new();
        let mut errors = Vec::new();

        let deadline = Instant::now() + self.config.overall_timeout;

        while let Some(result) = futures.next().await {
            match result {
                Ok((peer, blocks)) => {
                    // 记录成功
                    self.peer_scorer.write().await.record_request(
                        peer,
                        Some(blocks.fetch_latency_ms),
                        true,
                    );
                    results.extend(blocks.blocks);

                    // 启动下一个请求
                    if let Some(chunk) = chunk_iter.next() {
                        let next_peer = *peer_iter.next().unwrap();
                        futures.push(self.fetch_chunk(next_peer, chunk));
                    }
                }
                Err((peer, e)) => {
                    // 记录失败
                    self.peer_scorer.write().await.record_request(peer, None, false);
                    errors.push(e);

                    // 重试失败的 chunk
                    // TODO: 实现重试逻辑
                }
            }

            // 检查超时
            if Instant::now() > deadline {
                return Err(BlockRetrievalError::Timeout);
            }
        }

        if results.is_empty() && !errors.is_empty() {
            return Err(BlockRetrievalError::AllRequestsFailed(errors));
        }

        // 排序并去重
        results.sort_by_key(|b| b.round());
        results.dedup_by_key(|b| b.id());

        Ok(results)
    }

    fn create_request_chunks(
        &self,
        start_block_id: HashValue,
        num_blocks: u64,
        target_block_id: HashValue,
    ) -> Vec<BlockRetrievalChunk> {
        let chunk_size = self.config.max_blocks_per_request;
        let num_chunks = (num_blocks + chunk_size - 1) / chunk_size;

        (0..num_chunks)
            .map(|i| BlockRetrievalChunk {
                start_offset: i * chunk_size,
                num_blocks: chunk_size.min(num_blocks - i * chunk_size),
                start_block_id,
                target_block_id,
            })
            .collect()
    }

    async fn fetch_chunk(
        &self,
        peer: PeerId,
        chunk: BlockRetrievalChunk,
    ) -> Result<(PeerId, FetchResult), (PeerId, BlockRetrievalError)> {
        let start = Instant::now();

        let result = timeout(
            self.config.request_timeout,
            self.network.request_block(
                BlockRetrievalRequest::new(chunk.start_block_id, chunk.num_blocks),
                PeerNetworkId::new(NetworkId::Vfn, peer),
                self.config.request_timeout,
            ),
        )
        .await;

        let latency_ms = start.elapsed().as_millis() as u64;

        match result {
            Ok(Ok(response)) => Ok((peer, FetchResult {
                blocks: response.blocks().clone(),
                fetch_latency_ms: latency_ms,
            })),
            Ok(Err(e)) => Err((peer, BlockRetrievalError::NetworkError(e.to_string()))),
            Err(_) => Err((peer, BlockRetrievalError::Timeout)),
        }
    }
}

#[derive(Debug)]
struct BlockRetrievalChunk {
    start_offset: u64,
    num_blocks: u64,
    start_block_id: HashValue,
    target_block_id: HashValue,
}

struct FetchResult {
    blocks: Vec<Block>,
    fetch_latency_ms: u64,
}
```

### 4.3 区块预取

**优化方案：**

```rust
/// 区块预取器
pub struct BlockPrefetcher {
    /// 预取窗口大小（提前预取多少轮的区块）
    prefetch_window: u64,
    /// 预取缓存
    cache: Arc<RwLock<LruCache<HashValue, Block>>>,
    /// 后台任务句柄
    task_handle: Option<JoinHandle<()>>,
    /// 控制通道
    control_tx: mpsc::Sender<PrefetchCommand>,
}

enum PrefetchCommand {
    /// 提示当前轮次，触发预取
    Hint { current_round: u64, current_block_id: HashValue },
    /// 停止预取
    Stop,
}

impl BlockPrefetcher {
    pub fn new(prefetch_window: u64, cache_size: usize) -> Self {
        let cache = Arc::new(RwLock::new(LruCache::new(
            NonZeroUsize::new(cache_size).unwrap()
        )));
        let (control_tx, control_rx) = mpsc::channel(100);

        Self {
            prefetch_window,
            cache,
            task_handle: None,
            control_tx,
        }
    }

    pub fn start(&mut self, retriever: Arc<BlockRetriever>) {
        let cache = self.cache.clone();
        let window = self.prefetch_window;
        let (control_tx, mut control_rx) = mpsc::channel(100);
        self.control_tx = control_tx;

        self.task_handle = Some(tokio::spawn(async move {
            while let Some(cmd) = control_rx.recv().await {
                match cmd {
                    PrefetchCommand::Hint { current_round, current_block_id } => {
                        // 预取后续区块
                        for offset in 1..=window {
                            let target_round = current_round + offset;
                            // 检查缓存中是否已有
                            // 如果没有，发起预取请求
                            // ...
                        }
                    }
                    PrefetchCommand::Stop => break,
                }
            }
        }));
    }

    /// 提示当前进度，触发预取
    pub async fn hint(&self, current_round: u64, current_block_id: HashValue) {
        let _ = self.control_tx
            .send(PrefetchCommand::Hint { current_round, current_block_id })
            .await;
    }

    /// 从缓存获取区块
    pub async fn get_cached(&self, block_id: &HashValue) -> Option<Block> {
        self.cache.read().await.peek(block_id).cloned()
    }
}
```

## 5. 连接管理优化

### 5.1 连接池复用

```rust
/// 连接池
pub struct ConnectionPool {
    /// 每个 peer 的连接
    connections: RwLock<HashMap<PeerId, Vec<PooledConnection>>>,
    /// 每个 peer 的最大连接数
    max_connections_per_peer: usize,
    /// 空闲超时时间
    idle_timeout: Duration,
    /// 连接创建器
    connection_factory: Arc<dyn ConnectionFactory>,
}

struct PooledConnection {
    connection: Connection,
    last_used: Instant,
    request_count: u64,
}

#[async_trait]
trait ConnectionFactory: Send + Sync {
    async fn create(&self, peer: PeerId) -> Result<Connection>;
}

impl ConnectionPool {
    pub fn new(
        max_connections_per_peer: usize,
        idle_timeout: Duration,
        connection_factory: Arc<dyn ConnectionFactory>,
    ) -> Self {
        Self {
            connections: RwLock::new(HashMap::new()),
            max_connections_per_peer,
            idle_timeout,
            connection_factory,
        }
    }

    /// 获取或创建连接
    pub async fn acquire(&self, peer: PeerId) -> Result<PooledConnectionGuard> {
        // 1. 尝试从池中获取空闲连接
        {
            let mut connections = self.connections.write().await;
            if let Some(peer_conns) = connections.get_mut(&peer) {
                // 清理过期连接
                peer_conns.retain(|c| c.last_used.elapsed() < self.idle_timeout);

                // 获取可用连接
                if let Some(mut conn) = peer_conns.pop() {
                    if conn.connection.is_healthy() {
                        conn.last_used = Instant::now();
                        return Ok(PooledConnectionGuard {
                            pool: self,
                            peer,
                            connection: Some(conn),
                        });
                    }
                }
            }
        }

        // 2. 创建新连接
        let connection = self.connection_factory.create(peer).await?;
        let pooled = PooledConnection {
            connection,
            last_used: Instant::now(),
            request_count: 0,
        };

        Ok(PooledConnectionGuard {
            pool: self,
            peer,
            connection: Some(pooled),
        })
    }

    /// 归还连接到池
    async fn release(&self, peer: PeerId, mut connection: PooledConnection) {
        connection.request_count += 1;
        connection.last_used = Instant::now();

        let mut connections = self.connections.write().await;
        let peer_conns = connections.entry(peer).or_insert_with(Vec::new);

        if peer_conns.len() < self.max_connections_per_peer {
            peer_conns.push(connection);
        }
        // 如果池已满，连接将被丢弃
    }

    /// 定期清理空闲连接
    pub async fn cleanup(&self) {
        let mut connections = self.connections.write().await;
        for peer_conns in connections.values_mut() {
            peer_conns.retain(|c| c.last_used.elapsed() < self.idle_timeout);
        }
        connections.retain(|_, conns| !conns.is_empty());
    }
}

/// 连接守卫，确保连接被归还
pub struct PooledConnectionGuard<'a> {
    pool: &'a ConnectionPool,
    peer: PeerId,
    connection: Option<PooledConnection>,
}

impl<'a> PooledConnectionGuard<'a> {
    pub fn connection(&mut self) -> &mut Connection {
        &mut self.connection.as_mut().unwrap().connection
    }
}

impl<'a> Drop for PooledConnectionGuard<'a> {
    fn drop(&mut self) {
        if let Some(conn) = self.connection.take() {
            let pool = self.pool;
            let peer = self.peer;
            // 异步归还连接
            tokio::spawn(async move {
                pool.release(peer, conn).await;
            });
        }
    }
}
```

### 5.2 健康检查优化

```rust
/// Peer 健康检查器
pub struct PeerHealthChecker {
    /// 检查间隔
    check_interval: Duration,
    /// 不健康阈值（连续失败次数）
    unhealthy_threshold: u32,
    /// peer 失败计数
    peer_failures: RwLock<HashMap<PeerId, u32>>,
    /// peer 延迟历史
    peer_latencies: RwLock<HashMap<PeerId, VecDeque<Duration>>>,
    /// 延迟历史窗口大小
    latency_window_size: usize,
}

impl PeerHealthChecker {
    pub fn new(check_interval: Duration, unhealthy_threshold: u32) -> Self {
        Self {
            check_interval,
            unhealthy_threshold,
            peer_failures: RwLock::new(HashMap::new()),
            peer_latencies: RwLock::new(HashMap::new()),
            latency_window_size: 10,
        }
    }

    /// 检查单个 peer 健康状态
    pub async fn check_peer(&self, peer: PeerId, network: &NetworkSender) -> HealthStatus {
        let start = Instant::now();

        match network.ping(peer, Duration::from_secs(5)).await {
            Ok(_) => {
                let latency = start.elapsed();
                self.record_success(peer, latency).await;
                HealthStatus::Healthy { latency }
            }
            Err(e) => {
                let failures = self.record_failure(peer).await;
                if failures >= self.unhealthy_threshold {
                    HealthStatus::Unhealthy {
                        consecutive_failures: failures,
                        last_error: e.to_string(),
                    }
                } else {
                    HealthStatus::Degraded {
                        consecutive_failures: failures,
                    }
                }
            }
        }
    }

    async fn record_success(&self, peer: PeerId, latency: Duration) {
        // 清除失败计数
        self.peer_failures.write().await.remove(&peer);

        // 记录延迟
        let mut latencies = self.peer_latencies.write().await;
        let history = latencies.entry(peer).or_insert_with(VecDeque::new);
        if history.len() >= self.latency_window_size {
            history.pop_front();
        }
        history.push_back(latency);
    }

    async fn record_failure(&self, peer: PeerId) -> u32 {
        let mut failures = self.peer_failures.write().await;
        let count = failures.entry(peer).or_insert(0);
        *count += 1;
        *count
    }

    /// 获取 peer 平均延迟
    pub async fn get_average_latency(&self, peer: &PeerId) -> Option<Duration> {
        let latencies = self.peer_latencies.read().await;
        latencies.get(peer).map(|history| {
            let sum: Duration = history.iter().sum();
            sum / history.len() as u32
        })
    }

    /// 获取所有健康的 peers
    pub async fn get_healthy_peers(&self, all_peers: &[PeerId]) -> Vec<PeerId> {
        let failures = self.peer_failures.read().await;
        all_peers
            .iter()
            .filter(|p| {
                failures.get(p).map_or(true, |&f| f < self.unhealthy_threshold)
            })
            .cloned()
            .collect()
    }
}

#[derive(Debug, Clone)]
pub enum HealthStatus {
    Healthy { latency: Duration },
    Degraded { consecutive_failures: u32 },
    Unhealthy { consecutive_failures: u32, last_error: String },
}
```

## 6. 可观测性优化

### 6.1 新增网络指标

```rust
use prometheus::{
    register_histogram_vec, register_int_counter_vec, register_int_gauge_vec,
    HistogramVec, IntCounterVec, IntGaugeVec,
};

lazy_static! {
    // ===== VFN 同步指标 =====

    /// VFN 区块同步延迟
    pub static ref VFN_SYNC_LATENCY: HistogramVec = register_histogram_vec!(
        "gravity_vfn_sync_latency_seconds",
        "VFN block sync latency in seconds",
        &["peer_id", "status"],
        vec![0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
    ).unwrap();

    /// VFN 同步的区块数量
    pub static ref VFN_SYNC_BLOCKS: IntCounterVec = register_int_counter_vec!(
        "gravity_vfn_sync_blocks_total",
        "Total number of blocks synced via VFN",
        &["peer_id"]
    ).unwrap();

    // ===== Peer 连接指标 =====

    /// Peer 连接状态 (1=connected, 0=disconnected)
    pub static ref PEER_CONNECTION_STATUS: IntGaugeVec = register_int_gauge_vec!(
        "gravity_peer_connection_status",
        "Peer connection status",
        &["peer_id", "network_id"]
    ).unwrap();

    /// 活跃连接数
    pub static ref ACTIVE_CONNECTIONS: IntGaugeVec = register_int_gauge_vec!(
        "gravity_active_connections",
        "Number of active connections",
        &["network_id", "direction"]
    ).unwrap();

    // ===== 区块检索指标 =====

    /// 区块检索请求总数
    pub static ref BLOCK_RETRIEVAL_REQUESTS: IntCounterVec = register_int_counter_vec!(
        "gravity_block_retrieval_requests_total",
        "Total block retrieval requests",
        &["peer_id", "status"]
    ).unwrap();

    /// 区块检索延迟
    pub static ref BLOCK_RETRIEVAL_LATENCY: HistogramVec = register_histogram_vec!(
        "gravity_block_retrieval_latency_seconds",
        "Block retrieval latency",
        &["peer_id"],
        vec![0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
    ).unwrap();

    /// 每个 peer 的检索成功率
    pub static ref PEER_RETRIEVAL_SUCCESS_RATE: IntGaugeVec = register_int_gauge_vec!(
        "gravity_peer_retrieval_success_rate_percent",
        "Block retrieval success rate per peer (0-100)",
        &["peer_id"]
    ).unwrap();

    // ===== 重试指标 =====

    /// 重试次数分布
    pub static ref RETRIEVAL_RETRY_COUNT: HistogramVec = register_histogram_vec!(
        "gravity_retrieval_retry_count",
        "Number of retries per retrieval",
        &["outcome"],  // success, failure
        vec![0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    ).unwrap();
}

/// 指标记录辅助函数
pub struct NetworkMetrics;

impl NetworkMetrics {
    pub fn record_sync_latency(peer: &str, status: &str, latency: Duration) {
        VFN_SYNC_LATENCY
            .with_label_values(&[peer, status])
            .observe(latency.as_secs_f64());
    }

    pub fn increment_sync_blocks(peer: &str, count: u64) {
        VFN_SYNC_BLOCKS
            .with_label_values(&[peer])
            .inc_by(count);
    }

    pub fn set_connection_status(peer: &str, network: &str, connected: bool) {
        PEER_CONNECTION_STATUS
            .with_label_values(&[peer, network])
            .set(if connected { 1 } else { 0 });
    }

    pub fn record_retrieval(peer: &str, success: bool, latency: Duration) {
        let status = if success { "success" } else { "failure" };
        BLOCK_RETRIEVAL_REQUESTS
            .with_label_values(&[peer, status])
            .inc();

        if success {
            BLOCK_RETRIEVAL_LATENCY
                .with_label_values(&[peer])
                .observe(latency.as_secs_f64());
        }
    }
}
```

## 7. 错误处理优化

### 7.1 细粒度错误类型

```rust
use thiserror::Error;

/// 网络层错误
#[derive(Debug, Error)]
pub enum NetworkError {
    #[error("Peer not found: {0}")]
    PeerNotFound(PeerId),

    #[error("Connection failed to peer {peer}: {reason}")]
    ConnectionFailed {
        peer: PeerId,
        reason: String,
    },

    #[error("Connection timeout to peer {peer} after {duration:?}")]
    ConnectionTimeout {
        peer: PeerId,
        duration: Duration,
    },

    #[error("Handshake failed with peer {peer}: {reason}")]
    HandshakeFailed {
        peer: PeerId,
        reason: String,
    },

    #[error("Protocol negotiation failed: {0}")]
    ProtocolNegotiationFailed(String),

    #[error("Discovery failed: {0}")]
    DiscoveryFailed(#[from] DiscoveryError),

    #[error("Block retrieval failed: {0}")]
    BlockRetrievalFailed(#[from] BlockRetrievalError),
}

/// 发现层错误
#[derive(Debug, Error)]
pub enum DiscoveryError {
    #[error("File discovery failed: {path}: {reason}")]
    FileDiscoveryFailed {
        path: PathBuf,
        reason: String,
    },

    #[error("On-chain discovery failed: {0}")]
    OnChainDiscoveryFailed(String),

    #[error("No peers discovered")]
    NoPeersDiscovered,
}

/// 区块检索错误
#[derive(Debug, Error)]
pub enum BlockRetrievalError {
    #[error("Block not found: {0}")]
    BlockNotFound(HashValue),

    #[error("Quorum certificate not found for block: {0}")]
    QCNotFound(HashValue),

    #[error("All peers exhausted after {attempts} attempts")]
    AllPeersExhausted {
        attempts: u32,
    },

    #[error("No peers available for retrieval")]
    NoPeersAvailable,

    #[error("Retrieval timeout after {duration:?}")]
    Timeout {
        duration: Duration,
    },

    #[error("Invalid block signature from peer {peer}")]
    InvalidSignature {
        peer: PeerId,
    },

    #[error("Block validation failed: {reason}")]
    ValidationFailed {
        reason: String,
    },

    #[error("Network error: {0}")]
    NetworkError(String),

    #[error("All requests failed: {0:?}")]
    AllRequestsFailed(Vec<BlockRetrievalError>),
}

impl BlockRetrievalError {
    /// 是否可重试
    pub fn is_retryable(&self) -> bool {
        matches!(
            self,
            Self::Timeout { .. } | Self::NetworkError(_)
        )
    }

    /// 是否应该换 peer 重试
    pub fn should_try_different_peer(&self) -> bool {
        matches!(
            self,
            Self::BlockNotFound(_) |
            Self::QCNotFound(_) |
            Self::InvalidSignature { .. } |
            Self::Timeout { .. }
        )
    }
}
```

## 8. 优化优先级矩阵

| 优先级 | 优化项 | 影响 | 工作量 | 风险 |
|-------|-------|------|-------|------|
| **P0** | VFN 角色状态机正式化 | 修复临时方案，提高稳定性 | 中 | 低 |
| **P0** | 移除 unwrap/expect | 提高可靠性，避免崩溃 | 低 | 低 |
| **P1** | 智能 Peer 选择 | 提高同步速度 20-30% | 中 | 低 |
| **P1** | 增加网络指标 | 提高可观测性 | 低 | 低 |
| **P1** | 细粒度错误类型 | 便于调试和监控 | 低 | 低 |
| **P2** | 配置可配置化 | 提高灵活性 | 低 | 低 |
| **P2** | 并行区块获取 | 大幅提高同步速度 | 高 | 中 |
| **P2** | 连接池复用 | 减少资源消耗 | 中 | 中 |
| **P3** | 区块预取 | 减少同步延迟 | 高 | 中 |
| **P3** | 统一发现机制 | 简化配置 | 中 | 中 |
| **P3** | 健康检查优化 | 提高网络稳定性 | 中 | 低 |

## 9. 快速见效的改进

如果时间有限，建议优先实施以下改进：

### 9.1 立即可做 (1-2 天)

```rust
// 1. 改进日志和错误信息 (sync_manager.rs)
if let Err(e) = self.retrieve_block(...).await {
    error!(
        peer = %peer,
        block_id = %block_id,
        error = %e,
        retries = cur_retry,
        "Block retrieval failed, trying next peer"
    );
    // 记录指标
    NetworkMetrics::record_retrieval(&peer.to_string(), false, start.elapsed());
}

// 2. 添加关键指标 (network.rs)
pub fn on_peer_connected(peer: PeerId, network_id: NetworkId) {
    PEER_CONNECTION_STATUS
        .with_label_values(&[&peer.to_string(), network_id.as_str()])
        .set(1);
    ACTIVE_CONNECTIONS
        .with_label_values(&[network_id.as_str(), "outbound"])
        .inc();
}

// 3. 改进配置验证 (bootstrap.rs)
pub fn validate_network_config(config: &NodeConfig) -> Result<(), NetworkInitError> {
    if config.base.role == RoleType::Validator {
        config.validator_network
            .as_ref()
            .ok_or(NetworkInitError::ValidatorNetworkNotConfigured)?;
    }
    Ok(())
}
```

### 9.2 短期可做 (1-2 周)

1. 实现 `PeerScorer` 智能选择
2. 添加完整的网络指标
3. 重构错误处理

### 9.3 中期可做 (1-2 月)

1. 实现并行区块获取
2. 实现连接池
3. 正式化 VFN 状态机
