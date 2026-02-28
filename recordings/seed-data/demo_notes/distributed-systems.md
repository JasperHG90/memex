# Distributed Systems Fundamentals

## Overview

Distributed systems are collections of independent computers that appear as a single coherent system to end users. They are foundational to modern infrastructure, powering everything from databases and message queues to machine learning training pipelines and knowledge management systems.

## The CAP Theorem

The CAP theorem, formulated by Eric Brewer, states that a distributed data store can provide at most two of the following three guarantees simultaneously:

- **Consistency**: Every read receives the most recent write or an error.
- **Availability**: Every request receives a non-error response, though it may not reflect the most recent write.
- **Partition Tolerance**: The system continues to operate despite network partitions between nodes.

In practice, network partitions are inevitable in distributed systems, so the real trade-off is between consistency and availability during a partition. Systems like PostgreSQL prioritize consistency (CP), while systems like DynamoDB lean toward availability (AP).

## Consensus Protocols

Consensus protocols enable distributed nodes to agree on a single value or sequence of operations, even in the presence of failures.

### Paxos

Paxos, designed by Leslie Lamport, was the first proven consensus protocol. It operates in two phases:

1. **Prepare phase**: A proposer sends a prepare request with a proposal number. Acceptors promise not to accept proposals with lower numbers.
2. **Accept phase**: If the proposer receives promises from a majority, it sends an accept request with the value.

While theoretically elegant, Paxos is notoriously difficult to implement correctly. Its multi-decree variant (Multi-Paxos) is used in production systems like Google's Chubby lock service.

### Raft

Raft was designed as an understandable alternative to Paxos. It decomposes consensus into three sub-problems:

- **Leader election**: Nodes elect a leader using randomized timeouts. Only one leader exists per term.
- **Log replication**: The leader accepts client requests, appends them to its log, and replicates to followers.
- **Safety**: Raft guarantees that committed entries are durable and will appear in the logs of all future leaders.

Raft is widely adopted in modern distributed systems, including etcd (used by Kubernetes), CockroachDB, and TiKV. Its clarity makes it the protocol of choice for new distributed database implementations.

## Replication Strategies

Distributed databases use different replication strategies depending on their consistency requirements:

- **Synchronous replication**: The leader waits for acknowledgment from all (or a quorum of) replicas before confirming a write. Provides strong consistency but higher latency.
- **Asynchronous replication**: The leader confirms writes immediately and replicates in the background. Lower latency but risks data loss during failures.
- **Semi-synchronous replication**: A hybrid approach where the leader waits for at least one replica to acknowledge, balancing durability and performance.

PostgreSQL supports all three modes, making it versatile for different deployment scenarios, from single-node development to multi-region production setups.

## Distributed Storage Patterns

### Sharding

Sharding partitions data across multiple nodes based on a shard key. This enables horizontal scaling but introduces complexity in cross-shard queries and rebalancing. Consistent hashing minimizes data movement when nodes are added or removed.

### Event Sourcing

Event sourcing stores all changes as an immutable sequence of events rather than mutable state. This pattern is natural for systems that need complete audit trails or temporal queries. It aligns well with append-only architectures where new data creates new entries rather than modifying existing ones.

### CQRS (Command Query Responsibility Segregation)

CQRS separates read and write operations into different models. Write operations go through a command model optimized for validation and consistency, while reads are served from a query model optimized for retrieval performance. This pattern is particularly effective when combined with event sourcing and is commonly used in knowledge management and search systems where write patterns differ significantly from read patterns.

## Practical Applications

Modern distributed systems combine these patterns in sophisticated ways. A knowledge management system might use PostgreSQL with pgvector for vector similarity search, append-only storage for immutable notes, and a queue-based reflection system that uses row-level locking (`SELECT ... FOR UPDATE SKIP LOCKED`) to distribute work across nodes without conflicts.
