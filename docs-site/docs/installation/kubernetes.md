---
id: install-kubernetes
title: Installation on Kubernetes
sidebar_position: 5
slug: /installation/kubernetes
---

# Installation on Kubernetes

:::info Work in progress
This page is being written. Check back soon.
:::

Prerequisites:

- Kubernetes 1.28+ cluster
- Helm 3.14+
- PersistentVolume provisioner

Quick start:

```bash
helm repo add solomon https://charts.solomon-labs.com
helm install harness solomon/harness \
  --set persistence.enabled=true \
  --set service.type=ClusterIP
```

The Helm chart deploys Harness API server, PostgreSQL, Qdrant, and OpenSearch as
separate pods with persistent storage.
