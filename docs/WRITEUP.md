# Writeup

This covers the five required sections: how to run it, the significant decisions and
why, what minikube quietly did for me, what's missing before this is production-grade,
and one incident runbook.

## 1. Run it

### Prerequisites

- macOS with Homebrew, or any machine with `docker`/`colima`, `minikube`, `kubectl`,
  `kubeseal`, and `go` (only needed if you want to rebuild the API image).
- A container runtime for minikube's `docker` driver. On Apple Silicon there's no
  Docker daemon by default, so I run one via Colima (`brew install colima docker` then
  `colima start`).

### Bring-up, from nothing

```bash
# 1. Runtime + cluster
colima start --cpu 4 --memory 8 --disk 60
minikube start --profile qoves --nodes=2 --cni=calico --driver=docker \
  --cpus=3 --memory=3800 --kubernetes-version=stable

# 2. Addons the cluster needs at the infra layer
minikube -p qoves addons enable ingress
minikube -p qoves addons enable metrics-server

# 3. Cluster bootstrap tooling (installed by hand, per the brief — everything
#    that comes AFTER this is GitOps-managed, not these controllers themselves)
kubectl apply -f https://github.com/bitnami-labs/sealed-secrets/releases/download/v0.26.3/controller.yaml
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/v2.11.4/manifests/install.yaml

# 4. Build and load the API image (no registry in this exercise, so the image
#    is built locally and loaded straight into both node's container runtimes)
cd app && docker build -t qoves-api:1.0.0 . && cd ..
minikube image load qoves-api:1.0.0 -p qoves

# 5. Seal the DB credentials against the running controller's public key
#    (see manifests/postgres/sealedsecret.yaml and manifests/api/sealedsecret.yaml —
#    this step only needs to happen once; the ciphertext is what lives in git)
kubeseal --fetch-cert --controller-name=sealed-secrets-controller \
  --controller-namespace=kube-system > pub-cert.pem
kubectl create secret generic postgres-credentials -n app --dry-run=client -o yaml \
  --from-literal=POSTGRES_DB=appdb --from-literal=POSTGRES_USER=appuser \
  --from-literal=POSTGRES_PASSWORD=<generated> | kubeseal --cert pub-cert.pem -o yaml \
  > manifests/postgres/sealedsecret.yaml
# (same pattern for manifests/api/sealedsecret.yaml with a DATABASE_URL key)

# 6. Point the root Application at this repo and apply it once, by hand —
#    this is the ONLY manifest ever applied imperatively. Everything downstream
#    of it is reconciled by ArgoCD.
kubectl apply -f argocd/root-app.yaml
```

From here ArgoCD takes over: the `root` Application watches `argocd/apps/` in this
repo, and each file there is itself an Application pointing at one directory under
`manifests/`. That's the app-of-apps: one root sync fans out into namespace,
postgres, api, and monitoring, each reconciled independently.

### Repo layout

```
app/                   # provided: API source + Dockerfile
argocd/
  root-app.yaml         # the one thing applied by hand
  apps/                 # child Applications — one per component
manifests/
  namespace/            # the `app` namespace
  postgres/             # StatefulSet, headless Service, NetworkPolicy, SealedSecret
  api/                  # Deployment, Service, Ingress, HPA, NetworkPolicies, SealedSecret
  monitoring/           # Prometheus (RBAC, config, Deployment, Service)
docs/
  WRITEUP.md            # this file
```

### Making a change (the GitOps flow)

Say I want to bump the API to two more replicas. I edit
`manifests/api/deployment.yaml`, commit, push to `main`. ArgoCD's polling loop (or
a manual `argocd app sync api`, or a webhook in a real setup) picks up the diff,
diffs live state against git, and applies it. I never run `kubectl edit` or
`kubectl apply` against a live workload — if I did, ArgoCD's self-heal would revert
it on the next reconcile, which is the point: git is the only path in.

## 2. Decisions

**CNI: Calico, not the minikube default.**
minikube's default CNI (a thin bridge setup) does not enforce `NetworkPolicy` at
all — policies apply cleanly and silently do nothing, which is worse than no
policy because it looks enforced in `kubectl get netpol` when it isn't. Calico and
Cilium both enforce policy properly; I picked Calico because it's the more
conservative, better-understood choice for a plain L3 policy story (no need for
Cilium's eBPF-based L7 policy or its Hubble observability layer here), and it's
what minikube documents directly (`--cni=calico`). Tradeoff: Calico adds real
resource overhead on a laptop-sized cluster (a controller pod plus a node agent
per node), which is part of why I kept the cluster to 2 nodes and modest
CPU/memory rather than 3+.

**GitOps controller: ArgoCD, not Flux.**
Both satisfy the requirement. I chose ArgoCD mainly for the app-of-apps pattern
being a first-class, well-documented idiom (a plain `Application` CRD pointing at
a directory of other `Application` manifests) and for the UI/CLI being easier to
demo live in a walkthrough — `argocd app diff`, `argocd app history`, and the
sync status tree map directly to "prove GitOps is doing the work" without me
narrating YAML. Flux's kustomize-controller + source-controller split is arguably
more composable for a larger multi-team setup, but for one small stack ArgoCD's
single-CRD model is easier to defend end-to-end.

**Secrets: Sealed Secrets, not SOPS or ESO.**
Sealed Secrets is the only option of the three that needs nothing outside the
cluster — no age/PGP key management process, no External Secrets Operator plus a
backing store to stand up. The controller holds a private key in-cluster and only
it can decrypt; what's in git is ciphertext specific to that cluster's key, so a
leaked repo leaks nothing. The real cost is that a cluster rebuild without a
backed-up controller key invalidates every `SealedSecret` in the repo — sealed
ciphertext is bound to one controller's key pair, not portable across clusters
by default. In production I'd back up that key (`kubectl get secret -n kube-system
sealed-secrets-key -o yaml`) to a store outside the cluster, or move to External
Secrets Operator backed by a real Vault/cloud KMS once there's infrastructure to
run one.

**Postgres: a raw StatefulSet, not CloudNativePG.**
A raw StatefulSet is the simplest thing that satisfies "state survives a restart,"
and it's what I can fully explain line by line without leaning on an operator's
defaults. CloudNativePG gives you failover, PITR, and backup scheduling as
first-class CRDs, which is genuinely the better answer for anything that has to
survive a node failure without a human — but that's exactly the kind of
gold-plating the brief warns against when a single-instance dev Postgres is the
actual requirement. If this service needed real HA I'd reach for CloudNativePG
next, not roll my own failover.

**Scaling signal: CPU-based HPA, with a caveat.**
I wired a CPU-utilization HPA because it's the one signal `metrics-server` gives
you for free and the assignment asks for an HPA to exist. It is not, however, the
right signal for this API: `/`, `/healthz`, and `/metrics` are all cheap handlers
whose cost is dominated by a Postgres round trip, not CPU. A request-storm on this
service would show up as connection pool exhaustion and rising p99 latency long
before CPU utilization moved meaningfully. A real deployment of this service
should scale on request rate or in-flight requests (Prometheus Adapter exposing a
custom metric to the HPA, or KEDA against the same signal) — CPU-based scaling
here is a placeholder that satisfies the requirement, not a recommendation.
One operational detail worth calling out: the API's `Application` sets
`ignoreDifferences` on the Deployment's `/spec/replicas`. Without it, ArgoCD's
`selfHeal` would treat every HPA-driven scale-up as drift from the 2 replicas
committed in git and scale it straight back down — GitOps and an HPA fighting
over the same field is a real footgun, not a hypothetical one.

**Probe design: readiness checks the database, liveness doesn't.**
`readinessProbe` hits `/healthz`, which does the real `SELECT 1` round trip;
`livenessProbe` hits `/`, which touches nothing but the process itself. This is
deliberate: it's what makes startup ordering safe without a Kubernetes-level
`initContainer` wait-for-postgres hack — a pod that starts before Postgres is
reachable simply never becomes `Ready` and gets no traffic, instead of crash-
looping. If `/healthz` were also the liveness check, a transient DB blip (a
Postgres restart, a brief network hiccup) would kill and restart otherwise-
healthy API pods for a problem restarting them doesn't fix, which is the classic
mistake of conflating "can't serve this one dependency" with "this process is
broken."

**Storage: what the PVC's access mode means, and what happens on failure.**
The Postgres PVC uses `ReadWriteOnce` (minikube's default `standard` StorageClass,
backed by `hostPath` under the hood), which means the volume can only be mounted
by one node at a time — that's fine for a single-writer StatefulSet, but it
pins the pod to whichever node currently holds that volume; the pod cannot be
rescheduled to another node without the volume following it, and on minikube's
`hostPath` provisioner it physically can't, because the data lives on one node's
disk. If that node dies, the volume — and the data on it — dies with it, full
stop; there's no replication underneath a single-node `hostPath` PV. On real
cloud storage (EBS, PD) a `ReadWriteOnce` volume survives node loss because the
disk is a detachable network resource, which `hostPath` is not. Backup/restore
here would be `pg_dump` on a schedule to object storage outside the cluster
(or a `CronJob` running `pg_basebackup`); restore is spinning up a fresh Postgres
pod against an empty PVC and replaying the dump before opening it to traffic.
This gap — no off-node backup — is the single biggest storage risk in this build
and is called out again in Production gaps below.

## 3. What minikube did for me

Things minikube handed me for free that I'd otherwise have had to build:

- **Control-plane bootstrap.** `kube-apiserver`, `etcd`, `controller-manager`,
  `scheduler` all come up correctly wired (certs, RBAC bootstrap tokens, static
  pod manifests) with one command. On bare metal that's kubeadm plus getting the
  cert hierarchy and etcd cluster right by hand.
- **CNI install.** Even with `--cni=calico`, minikube handles fetching and
  applying the Calico manifests against the right pod CIDR for the cluster it
  just created. On bare metal I'd be reconciling the CNI's pod CIDR against
  `--pod-network-cidr` myself and debugging why nodes stay `NotReady` when they
  don't match.
- **Ingress load-balancing.** The `ingress` addon gives me a working
  ingress-nginx plus a path to the node's IP. On bare metal, "no cloud load
  balancer" means I'd need something like MetalLB handing out real LAN IPs via
  ARP/BGP before ingress-nginx has anything to bind to externally.
- **The storage provisioner.** The `standard` StorageClass and its `hostPath`
  provisioner mean `PersistentVolumeClaim` just works. Bare metal has no default
  provisioner at all — I'd be running something like Rook/Ceph or local-path-
  provisioner myself, and accepting all the same single-node caveats explicitly
  rather than getting them silently from `hostPath`.
- **etcd plus its backup.** minikube's etcd is a single instance with no backup
  job and no story for corruption. Bare metal self-managed Kubernetes means I own
  etcd's disk performance, its 3-or-5-node quorum, and a `etcdctl snapshot save`
  cron job going somewhere durable — losing etcd is losing the cluster's entire
  state, not just one workload's.

## 4. Production gaps

What's missing before this serves real traffic, roughly in the order I'd close
these gaps:

1. **No HA anywhere.** One control-plane node, one Postgres instance, one
   Prometheus. A node reboot during a bad moment takes down the whole stack.
   Real HA means a 3-node (or managed) control plane, Postgres via CloudNativePG
   with a standby replica, and running critical add-ons with `PodDisruptionBudget`s.
2. **No off-cluster backups.** Covered above — `hostPath` PVC data does not
   survive node loss, and there is no scheduled `pg_dump`/`pg_basebackup` to
   object storage. This is the single highest-priority gap.
3. **Secrets backend is cluster-local.** Sealed Secrets' private key lives only
   in this cluster. A real secret backend (Vault, cloud KMS + External Secrets
   Operator) decouples secret material from any one cluster's lifecycle and adds
   audit logging on every read, which Sealed Secrets doesn't give you.
4. **No upgrade story.** I pinned Kubernetes, Calico, ArgoCD, and every image by
   tag or digest, but there's no tested path for rolling any of those forward —
   no staging cluster to validate a Kubernetes minor bump against, no documented
   rollback if an ArgoCD upgrade breaks a CRD.
5. **Single cluster, single region.** No multi-cluster failover, no story for
   what happens if the one cluster's underlying host/region has an outage.
6. **Observability is minimal by design.** One Prometheus, one alert, no
   Alertmanager routing to a real paging system, no long-term metrics storage
   (retention is 6h on `emptyDir`), no logs pipeline at all. Fine for this
   exercise; not fine for anything with an on-call rotation.
7. **Supply chain is unverified.** Images are pinned by tag/digest but nothing
   stops an unsigned or untrusted image from running — no admission policy, no
   image signing/verification (cosign + Kyverno/OPA Gatekeeper would close this).

## 5. Runbook: the Postgres pod dies

**Scenario:** the `postgres-0` pod crashes or is evicted, and either comes back
degraded or the node it was scheduled to is gone.

1. **Detect.** The `APITargetDown`-style signal on the API won't catch this
   directly — it's `/healthz` returning 503 that will (DB unreachable), and
   `kubectl get pods -n app` showing `postgres-0` not `Running`. In a fuller
   build I'd have a second alert directly on the StatefulSet's ready replica
   count; noted as a gap above.
2. **Confirm blast radius.**
   ```bash
   kubectl -n app get pods,pvc
   kubectl -n app describe pod postgres-0
   kubectl -n app logs postgres-0 --previous
   ```
   Distinguish "container crashed, same node, same PVC" (StatefulSet will just
   restart it and the PVC re-attaches) from "node is gone" (the pod is stuck
   `Pending` because the `hostPath` PVC can't follow it to another node — see
   the storage ADR above; this is the scenario that actually needs intervention).
3. **If it's a simple crash:** do nothing manually. The StatefulSet controller
   restarts the pod, it re-mounts the same PVC, Postgres replays its WAL, and
   `/healthz` recovers on its own. Confirm with:
   ```bash
   kubectl -n app rollout status statefulset/postgres
   curl -s http://api.qoves.local/healthz
   ```
4. **If the node is gone and the PVC is unrecoverable:** this is a data-loss
   event given the current single-node `hostPath` storage — there is no replica
   to fail over to. Recovery is restore-from-backup, which today means: this
   gap is real and the honest answer is "we lose data since there's no scheduled
   backup yet." The fix, done through git: add a `CronJob` manifest under
   `manifests/postgres/` running `pg_dump` to an object store, commit it, let
   ArgoCD reconcile it in — this closes the gap for next time, not this
   incident.
5. **If a backup did exist:** provision a fresh PVC (new `postgres-0`, since
   StatefulSet PVCs aren't deleted automatically — I'd need to delete the old
   PVC explicitly if the underlying volume is confirmed gone), let the
   StatefulSet come up against the empty volume, then restore the dump before
   pointing the API back at it. None of this is a `kubectl apply` against the
   live cluster except the emergency restore Job itself — the StatefulSet
   definition, the PVC, and the eventual scheduled backup all stay git-defined so
   the next reviewer can see exactly what changed and why, in the commit log
   rather than in someone's shell history.
6. **Postmortem action:** the actual fix isn't "restart the pod" — it's closing
   gap #2 above (real backups) and probably #1 (a standby replica via
   CloudNativePG) so this runbook stops being "we lost data" and starts being
   "we failed over in under a minute."
