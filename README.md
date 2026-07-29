# gitops-minikube-platform

A small HTTP API running on a self-managed Kubernetes cluster (minikube),
delivered entirely through GitOps: ArgoCD app-of-apps, default-deny
NetworkPolicies, secrets sealed at rest in git, PostgreSQL run by the
CloudNativePG operator (2-instance HA cluster with automated backups to
MinIO), an HPA, and a minimal Prometheus with one alert.

Full writeup - how to run it, the decisions and tradeoffs, what minikube gave
me for free, production gaps, a real chaos test with measured results, every
real bug I hit while building this, and an incident runbook - is in
[`docs/WRITEUP.md`](docs/WRITEUP.md).

## Layout

```
app/                    API source + Dockerfile
argocd/                 ArgoCD Application manifests (the app-of-apps)
manifests/              the actual Kubernetes manifests, one directory per component
  namespace/             the `app` namespace + its ResourceQuota
  minio/                 backup object store for Postgres
  postgres/              CloudNativePG Cluster, ScheduledBackup, NetworkPolicies
  api/                   Deployment, Service, Ingress, HPA, NetworkPolicies
  monitoring/            Prometheus + alert rule
docs/WRITEUP.md          the writeup
```

## Quickstart

```bash
minikube start --profile qoves --nodes=2 --cni=calico --driver=docker
minikube -p qoves addons enable ingress
minikube -p qoves addons enable metrics-server
minikube -p qoves addons enable csi-hostpath-driver
kubectl apply -f https://github.com/bitnami-labs/sealed-secrets/releases/download/v0.26.3/controller.yaml
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/v2.11.4/manifests/install.yaml
kubectl apply --server-side -f https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.24/releases/cnpg-1.24.1.yaml
kubectl apply -f argocd/root-app.yaml
```

See the writeup for the full sequence, including building the API image and
sealing the three credentials this stack needs - this quickstart assumes all
of that is already done.
