# System Architecture Overview

![System Diagram](system-diagram.png)

## Microservices Architecture

The platform uses a microservices architecture with 5 core services:

1. **API Gateway** — Routes requests using Kong with rate limiting and auth.
2. **User Service** — Manages authentication via OAuth2 and OIDC.
3. **Order Service** — Processes orders with CQRS and event sourcing patterns.
4. **Notification Service** — Sends emails and push notifications via AWS SES and FCM.
5. **Analytics Service** — Collects telemetry using OpenTelemetry and exports to Grafana.

## Communication

Services communicate via gRPC for synchronous calls and Apache Kafka for async events.
The event schema registry uses Protobuf for type-safe message contracts.

## Deployment

All services are containerized with Docker and deployed on Kubernetes (EKS).
Infrastructure is managed with Terraform and monitored with Prometheus + Grafana.
