---
title: Project Delta Overview
description: Project Delta uses Scala and Akka for distributed processing.
tags: [project, scala]
vault_name: bench-vault-b
---

# Project Delta — Distributed Processing Engine

**Lead:** Henrik Johansen
**Organization:** Nordic Data Systems

## Overview

Project Delta is a distributed data processing engine built at Nordic Data Systems.
The system uses Scala and the Akka framework for actor-based concurrency.
It processes 2 billion events per day across a 200-node cluster.

## Tech Stack

- Language: Scala 3.4
- Framework: Akka Cluster + Akka Streams
- Database: Apache Cassandra
- Orchestration: Apache Flink
