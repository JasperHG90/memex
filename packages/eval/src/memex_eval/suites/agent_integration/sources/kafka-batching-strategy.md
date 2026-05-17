---
tags: [kafka, platform, batching, alpha]
description: Kafka batching strategy decision — adopting time-based windows.
publish_date: 2025-09-12
---

# Kafka batching strategy — Project Alpha

**Date:** September 12, 2025
**Owner:** Sarah Chen
**Status:** decided

## Decision

We adopted a **time-based Kafka batching window of 250 ms** with a
soft cap of 500 messages per batch on the Project Alpha analytics
producer. The previous setting was size-only (1000 messages per
batch), which let small bursts sit in the producer buffer for tens
of seconds during low-traffic windows and broke our near-real-time
dashboards.

## Rationale

The Project Alpha analytics dashboard requires events to surface
within one second of being emitted. Under size-only batching at 1000
messages, off-peak windows sat at 5 to 30 messages per second and
the producer would wait minutes to fill a batch, blowing the SLA.

Time-based windows at 250 ms guarantee a flush cadence regardless of
load. The 500-message soft cap protects the broker from oversized
batches during bursts (we measured peak bursts at roughly 2000
messages per second, so 500 per batch corresponds to a flush every
250 ms in either regime).

## Implementation notes

- linger.ms = 250
- batch.size = 524288 (512 KiB; corresponds to ~500 typical messages)
- Producer acks = all
- Compression: zstd

## Open questions

The compaction interaction with time-based batches has not been
tested at scale. Sarah Chen will run a soak test in October before
we promote this setting to the marketing-events producer.
