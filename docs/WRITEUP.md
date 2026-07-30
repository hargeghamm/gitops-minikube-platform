# Writeup

Five required sections below, plus a section on real bugs I hit and fixed while
building this — because "the tool wrote it" isn't an answer, and the honest
version of this project includes the things that didn't work on the first try.

## 1. Run it

### Prerequisites

- macOS with Homebrew, or any machine with `docker`/`colima`, `minikube`,
  `kubectl`, and `kubeseal`.
- A container runtime for minikube's `docker` driver. On Apple Silicon there's
  no Docker daemon by default, so I run one via Colima
  (`brew install colima docker`, then `colima start`).

### Bring-up, from nothing

```bash
# 1. Runtime + cluster
colima start --cpu 4 --memory 8 --disk 60
minikube start --profile qoves --nodes=2 --cni=calico --driver=docker \
  --cpus=3 --memory=3800 --kubernetes-version=stable

# 2. Addons the cluster needs at the infra layer
minikube -p qoves addons enable ingress
minikube -p qoves addons enable metrics-server
# csi-hostpath-driver, not the default "standard" StorageClass - see the
# storage ADR below for why this specific choice matters, not just any CSI
minikube -p qoves addons enable csi-hostpath-driver

# 3. Cluster bootstrap tooling (installed by hand, per the brief - everything
#    that comes AFTER this is GitOps-managed, not these controllers themselves)
kubectl apply -f https://github.com/bitnami-labs/sealed-secrets/releases/download/v0.26.3/controller.yaml
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/v2.11.4/manifests/install.yaml
kubectl apply --server-side -f https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.24/releases/cnpg-1.24.1.yaml

# 4. Build and load the API image (no registry in this exercise, so the image
#    is built locally and loaded straight into both nodes' container runtimes)
cd app && docker build -t qoves-api:2.0.0 . && cd ..
minikube image load qoves-api:2.0.0 -p qoves

# 5. Seal the credentials this stack needs against the running controller's
#    public key (see manifests/*/sealedsecret*.yaml - this step only needs to
#    happen once; only ciphertext ever lands in git):
#      - postgres-owner-credentials (the CNPG cluster's app-user bootstrap)
#      - api-db-credentials (DATABASE_URL, built from the same password -
#        note sslmode=verify-full&sslrootcert=/etc/postgres-ca/ca.crt, not
#        sslmode=disable; see the TLS-in-transit ADR below for why)
#      - minio-credentials (the backup object store's root credentials,
#        reused by both MinIO's own container env and CNPG's s3Credentials)
kubeseal --fetch-cert --controller-name=sealed-secrets-controller \
  --controller-namespace=kube-system > pub-cert.pem
# ... kubectl create secret --dry-run=client -o yaml | kubeseal --cert pub-cert.pem ...

# 6. Point the root Application at this repo and apply it once, by hand -
#    this is the ONLY manifest ever applied imperatively. Everything downstream
#    of it is reconciled by ArgoCD.
kubectl apply -f argocd/root-app.yaml
```

From here ArgoCD takes over: the `root` Application watches `argocd/apps/` in
this repo, and each file there is itself an Application pointing at one
directory under `manifests/`. That's the app-of-apps: one root sync fans out
into namespace, minio, postgres, api, and monitoring, each reconciled
independently, in the order their `sync-wave` annotations say they need to be
(namespace first, then minio, then everything that depends on it).

### Repo layout

```
app/                    # provided: API source + Dockerfile
argocd/
  root-app.yaml          # the one thing applied by hand
  apps/                  # child Applications - one per component, sync-waved
manifests/
  namespace/             # the `app` namespace + its ResourceQuota
  minio/                 # backup object store: Deployment, PVC, sealed creds,
                          # NetworkPolicy, and a PostSync hook Job that creates
                          # the bucket (MinIO doesn't auto-create one)
  postgres/               # CNPG Cluster (2 instances), ScheduledBackup,
                          # sealed bootstrap credentials, NetworkPolicies
  api/                    # Deployment, Service, Ingress, HPA, NetworkPolicies,
                          # sealed DATABASE_URL
  monitoring/             # Prometheus (RBAC, config, Deployment, Service)
docs/
  WRITEUP.md              # this file
```

### Making a change (the GitOps flow)

Say I want to bump the API to two more max replicas. I edit
`manifests/api/hpa.yaml`, commit, push to `main`. ArgoCD's polling loop (or a
manual `argocd app sync api`, or a webhook in a real setup) picks up the diff,
diffs live state against git, and applies it. I never run `kubectl edit` or
`kubectl apply` against a live workload - if I did, ArgoCD's self-heal would
revert it on the next reconcile, which is the point: git is the only path in.
The one deliberate exception is `ignoreDifferences` on the api Deployment's
`/spec/replicas` - see the scaling ADR below for why that specific field, and
only that field, is excluded from self-heal.

## 2. Decisions

**CNI: Calico, not the minikube default - and not Cilium, with a caveat.**
minikube's default CNI (a thin bridge setup) does not enforce `NetworkPolicy`
at all - policies apply cleanly and silently do nothing, which is worse than
no policy because it looks enforced in `kubectl get netpol` when it isn't.
Calico and Cilium both enforce policy properly; I picked Calico as the more
conservative, better-understood choice for plain L3/L4 policy (no need for
Cilium's eBPF dataplane or its Hubble observability layer here). The honest
caveat: one of this assignment's stretch goals is "allow the app exactly one
external domain," and Cilium's `toFQDNs` is specifically built for that -
domain-aware egress rules. Calico OSS's `NetworkPolicy` only matches on IPs
and ports, not domain names (that's a Calico Enterprise feature). So my CNI
choice quietly forecloses that particular stretch goal; if I wanted it, Cilium
would have been the right call from the start, not Calico. I didn't attempt
that stretch here rather than bolt on a second CNI or fake it with a brittle
IP-allowlist that breaks the moment the target's IP rotates.

**GitOps controller: ArgoCD, not Flux.**
Both satisfy the requirement. I chose ArgoCD mainly for the app-of-apps
pattern being a first-class, well-documented idiom (a plain `Application` CRD
pointing at a directory of other `Application` manifests) and for the UI/CLI
being easier to demo live - `argocd app diff`, `argocd app history`, and the
sync status tree map directly to "prove GitOps is doing the work" without me
narrating YAML. Flux's kustomize-controller + source-controller split is
arguably more composable for a larger multi-team setup, but for one small
stack ArgoCD's single-CRD model is easier to defend end-to-end.

**Secrets: Sealed Secrets, not SOPS or ESO.**
Sealed Secrets is the only option of the three that needs nothing outside the
cluster - no age/PGP key management process, no External Secrets Operator plus
a backing store to stand up. The controller holds a private key in-cluster and
only it can decrypt; what's in git is ciphertext specific to that cluster's
key, so a leaked repo leaks nothing. The real cost is that a cluster rebuild
without a backed-up controller key invalidates every `SealedSecret` in the
repo - sealed ciphertext is bound to one controller's key pair, not portable
across clusters by default. In production I'd back up that key
(`kubectl get secret -n kube-system sealed-secrets-key -o yaml`) to a store
outside the cluster, or move to External Secrets Operator backed by a real
Vault/cloud KMS once there's infrastructure to run one. This build seals three
separate credentials (the CNPG owner bootstrap, the API's `DATABASE_URL`, and
MinIO's root credentials) - the mechanism doesn't get more complex per secret,
which is part of why it's the right choice at this scale.

Two properties of this setup are worth being explicit about rather than
leaving implicit:

- **Sealed Secrets ciphertext is bound to the exact namespace + secret name by
  default** (I didn't pass `--scope`, so the strict default applies). A leaked
  `SealedSecret` manifest can't be copy-pasted into a different namespace or
  renamed and still decrypt - it's cryptographically tied to where I actually
  intended it to land, not just "anything this cluster's key can open."
- **`DATABASE_URL` as a plain environment variable is the app's contract, not
  my preference.** Env vars for secrets are a real, commonly-cited anti-pattern
  (readable via `/proc/<pid>/environ` by anything sharing the container's PID
  namespace, trivially dumped by `kubectl exec ... env` to anyone with exec
  RBAC, prone to leaking into crash logs) - mounting as a file is generally
  better. I didn't get to choose here: the brief specifies the API "reads its
  connection string from the `DATABASE_URL` environment variable," and I
  wrote the app to that exact contract. Where I *did* have the choice - CNPG's
  own bootstrap credential - the operator never exposes it as a container env
  var at all: it reads the `Secret` via the Kubernetes API directly and
  applies the password with SQL, so it's never sitting in any container's
  environment, file, or process listing. That's a real secrets-hygiene
  advantage of choosing an operator over a hand-rolled StatefulSet that I
  didn't fully appreciate until I went looking for where CNPG actually
  consumes that secret.

**The Sealed Secrets controller is itself a high-value target, by design, and
that's a real tradeoff, not a footnote.** It holds the one private key that
can decrypt every `SealedSecret` in the cluster and necessarily has broad
`Secret` read/write RBAC across namespaces to materialize them - meaning
compromising that one controller is roughly equivalent to compromising every
secret this cluster manages. This is inherent to the architecture, not a
misconfiguration I introduced; it's the same tradeoff every "encrypt in git,
decrypt in-cluster" approach makes, and it's exactly why the key-backup
question two paragraphs up matters as much operationally as it does for
disaster recovery.

**Workload hardening: explicit `securityContext` and Pod Security Standards,
rolled out in a way that wouldn't have let a mistake reach `enforce` blind.**
Auditing this build specifically for security posture (not just "does it
work") surfaced a real finding: MinIO was running as **root** (`uid=0`) - the
official image's default, which I hadn't overridden because I'd only verified
functionality, not the container's actual identity. Neither the API nor MinIO
Deployment had any Kubernetes-level `securityContext` at all; the API's
non-root behavior existed only because the Dockerfile happens to set a `USER`
directive, with nothing at the cluster layer to catch a regression if that
ever changed. I fixed both: explicit `runAsNonRoot`, a specific `runAsUser`,
`allowPrivilegeEscalation: false`, dropped `ALL` capabilities, and
`seccompProfile: RuntimeDefault` on api, minio, and the bucket-creation hook
Job, plus `automountServiceAccountToken: false` on all three - none of them
talk to the Kubernetes API, so none of them need a token a compromised
container could try to use against it. Making MinIO non-root meant its PVC
needed the same `csi-hostpath-driver` StorageClass fix CNPG already needed
(`fsGroup` doesn't apply to `hostPath` volumes), which meant recreating that
PVC - fine, since it only held a demo backup, trivially regenerated.

Rather than assert these changes are correct, I rolled out namespace-wide Pod
Security Standards enforcement in the two steps the safe pattern actually
calls for: first `audit`+`warn` at the `restricted` level only (non-blocking -
surfaces violations without rejecting anything), then verified *every*
workload in the namespace against it by forcing real pod recreation, not just
reading the labels - a full `api` rollout, deleting MinIO's pod, and (by
accident, since the primary/replica roles had already swapped from the
earlier chaos test) two more live CNPG primary failovers. Zero PodSecurity
violations logged through any of it. Only then did I flip
`pod-security.kubernetes.io/enforce` to `restricted` and re-ran the same
recreation tests against actual enforcement, not just audit logging, to
confirm nothing that had passed the dry run then got rejected for real.
CNPG's own operator-managed pods, notably, were already fully
`restricted`-compliant out of the box (non-root, dropped capabilities,
read-only root filesystem, seccomp) without me touching their spec at all -
one more real advantage of the operator over anything I'd have hand-rolled.

**Secrets at rest in etcd: a real, documented gap I chose not to close on
this cluster, and why.** Sealed Secrets protects credentials *in git* - once
the controller decrypts a `SealedSecret` into a live Kubernetes `Secret`,
that object is stored in etcd like any other resource, and by default,
**Kubernetes does not encrypt `Secret` data at rest in etcd** - not on this
cluster, and not on most default installs. Anyone with direct access to
etcd's data files (or a snapshot of them) can read every secret in plaintext,
independent of any RBAC on the Kubernetes API itself. Closing this requires
an `EncryptionConfiguration` passed to the apiserver via
`--encryption-provider-config`, ideally using a KMS provider (envelope
encryption against a real external key management service) rather than the
weaker built-in `aescbc`/`secretbox` providers, which still leave the
encryption key sitting on the same control-plane disk as the data it
protects. I confirmed this gap is real on this specific cluster (`grep
encryption /etc/kubernetes/manifests/kube-apiserver.yaml` inside the minikube
VM returns nothing) and confirmed the fix is mechanically feasible - edit the
static pod manifest, mount a config file, the kubelet restarts the apiserver
automatically. I chose not to do it live: it means editing the running
apiserver's manifest on a cluster that already had one unplanned outage this
session (a laptop sleep took down the whole Colima VM mid-build) and one
stuck ArgoCD operation I had to manually clear - both fully recovered, but
real evidence that this specific environment's operational risk isn't
hypothetical today. This isn't something the brief asks for, and I'd rather
document a real gap precisely than gamble with a late, disruptive change to
a working, submission-ready cluster to close it. On a real cluster - or a
disposable throwaway profile - I'd do it and prove it with a fresh
`etcdctl` read showing ciphertext.

**Encryption in transit, API to Postgres: found disabled, fixed to
verified TLS.** A dedicated security pass over the whole stack turned up
something I'd carried forward unexamined since the raw-StatefulSet days:
`DATABASE_URL` had `sslmode=disable` baked in, meaning every query between
the API and Postgres travelled in plaintext on the wire - inside the
cluster network, but still plaintext, and still a real thing to check
rather than assume is fine because it's "internal." What made this an easy,
low-risk fix rather than a bigger project: CloudNativePG already
provisions a complete TLS PKI for the cluster by default (a CA plus a
server certificate covering every `postgres-*` service DNS name, and
mutually-authenticated client certificates for streaming replication
between instances - I found this by inspecting the actual generated
`pg_hba.conf` and `postgresql.conf` inside the running pod, not by reading
CNPG's docs and assuming). The server was already fully capable of
encrypted, verified connections; nothing was consuming it. The fix: mount
the CNPG-managed CA secret into the API pod and switch `DATABASE_URL` to
`sslmode=verify-full` - encrypted *and* hostname-verified against the
actual CA, not just "encrypted to something claiming to be the right
server." I confirmed this is genuinely active, not just configured: I ran
`SHOW ssl` against the exact connection the API uses and got `on`, and
separately confirmed `/healthz` still returns 200 - which it would only do
if `psycopg` successfully completed the TLS handshake and certificate
verification, since a verify-full failure raises an exception the endpoint
would surface as a 503 (I'd already seen that exact failure mode locally
while testing the container, so I know what it looks like). One thing
worth being honest about: replication traffic between CNPG's own primary
and replica was *already* using mutual TLS by default, entirely CNPG's
doing - the gap was specifically in the one connection string I authored
myself.

**RBAC review across every controller, not just the one I already fixed.**
I'd already scoped Prometheus's own RBAC down from a cluster-wide
`ClusterRole` to a namespaced `Role` (covered above). This pass, I checked
the *other* controllers' RBAC too: the Sealed Secrets controller's
`ClusterRole` genuinely does need `get/list/create/update/delete` on
`secrets` cluster-wide (confirmed by reading the full rule set, not the
truncated summary I glanced at first) - that's inherent to what the
controller has to do, not a misconfiguration, and matches what I'd already
written about it being a high-value target by design. CNPG's operator
`ClusterRole` is similarly broad (pods/exec, node reads, the ability to
create ServiceAccounts/Roles/RoleBindings) because it needs all of that to
run backup commands inside instances, schedule with topology awareness,
and provision per-cluster identities. Both are upstream-authored,
necessary for the controller to function, and not something I should
rewrite - the same judgment call as not touching ArgoCD's own install
manifest. Nothing to fix here; worth having actually checked rather than
assumed.

**Postgres: CloudNativePG, not a raw StatefulSet.**
I started with a raw StatefulSet - the simplest thing that satisfies "state
survives a restart" - and it worked. But "survives a pod restart" and "survives
losing the primary" are different claims, and only the operator makes the
second one true. CloudNativePG runs a real 2-instance cluster (one primary,
one streaming-replication standby) and manages failover, continuous WAL
archiving, and scheduled backups as CRDs instead of scripts I'd have to write
and maintain myself. I proved this isn't just configuration I copied - I
force-killed the primary pod twice during testing (see the Chaos section
below) and watched CNPG promote the standby automatically both times, with
zero data loss. The cost of this over a raw StatefulSet: more moving parts (an
operator, its webhooks, a backup object store), and a real dependency each
instance now has on reaching the Kubernetes API server directly for
coordination - which turned into one of the NetworkPolicy bugs described
below. If I only needed "restart-safe," the StatefulSet would have been the
right-sized answer; since the brief explicitly invites CloudNativePG as a
stretch and I have room in the three-day window, I built the more complete
answer and verified it, rather than describing it.

**Backup target: a single MinIO instance, not a managed object store.**
CloudNativePG's backup mechanism (barman-cloud) needs an S3-compatible
destination; MinIO is the obvious self-hosted choice for a dev cluster with no
cloud account in play. It's a single, unreplicated instance with no HA of its
own - which means the backup subsystem currently has exactly the single-point-
of-failure problem I built CloudNativePG to get Postgres itself out of. That's
a real, acknowledged gap, not an oversight - see Production gaps.

**Scaling signal: CPU-based HPA, with a caveat, plus a real GitOps/HPA
conflict I had to fix.**
I wired a CPU-utilization HPA because it's the one signal `metrics-server`
gives you for free and the assignment asks for an HPA to exist. It is not,
however, the right signal for this API: `/`, `/healthz`, and `/metrics` are
all cheap handlers whose cost is dominated by a Postgres round trip, not CPU.
A request-storm on this service would show up as connection pool exhaustion
and rising p99 latency long before CPU utilization moved meaningfully. A real
deployment of this service should scale on request rate or in-flight requests
(Prometheus Adapter exposing a custom metric to the HPA, or KEDA against the
same signal) - CPU-based scaling here is a placeholder that satisfies the
requirement, not a recommendation. Separately: the api `Application` sets
`ignoreDifferences` on the Deployment's `/spec/replicas`. Without it, ArgoCD's
`selfHeal` would treat every HPA-driven scale-up as drift from the 2 replicas
committed in git and scale it straight back down to 2 on the next reconcile -
GitOps and an HPA fighting over the same field is a real footgun I found by
reasoning through what self-heal actually diffs, not something I read about.

**Probe design: readiness checks the database, liveness doesn't.**
`readinessProbe` hits `/healthz`, which does the real `SELECT 1` round trip;
`livenessProbe` hits `/`, which touches nothing but the process itself. This
is deliberate: it's what makes startup ordering safe without a Kubernetes-level
`initContainer` wait-for-postgres hack - a pod that starts before Postgres is
reachable simply never becomes `Ready` and gets no traffic, instead of crash-
looping. If `/healthz` were also the liveness check, a transient DB blip (a
Postgres restart, a brief network hiccup, exactly the kind of thing this build
now deliberately induces and recovers from) would kill and restart otherwise-
healthy API pods for a problem restarting them doesn't fix.

**ResourceQuota + rollout safety.**
The `app` namespace has a `ResourceQuota` sized against the actual worst-case
simultaneous footprint of everything that can run in it - api at its HPA max
plus one rollout surge, both CNPG instances, MinIO, and the transient bucket
job (the math is in a comment in `manifests/namespace/resourcequota.yaml`, not
a round number picked by feel). The headroom isn't even across dimensions:
~1.8x on `requests.cpu`, ~1.4x on both memory dimensions, and only ~1.2x on
`limits.cpu` - that last one is the tightest margin, and the one I'd widen
first if this namespace ever needed to run one more transient thing (a debug
pod, a one-off Job) while the HPA and a rollout were both maxed out
simultaneously. The api Deployment's rollout strategy is explicit
(`maxSurge: 1, maxUnavailable: 0`) specifically so a rolling update's extra pod
never gets rejected by that quota. I proved this isn't just asserted: I
triggered a real rollout (`kubectl rollout restart deployment/api`) against
the live quota and it completed cleanly.

**Storage: the three questions the brief asks for, answered directly.**

*What access mode does the DB volume use, and what does it constrain about
scheduling?* `ReadWriteOnce`, on `csi-hostpath-driver`'s `csi-hostpath-sc`
StorageClass - deliberately not minikube's default `standard` class (see the
bug below). `ReadWriteOnce` means each volume can be mounted by one node at a
time, which pins that pod to whichever node currently holds it - a Postgres
instance can't be freely rescheduled elsewhere without its volume following
it. CNPG's two instances each get their own separate `ReadWriteOnce` volume,
which is what turns "one node's worth of storage" into real redundancy: they
aren't forced onto the same node, so one node holding one instance's volume
doesn't constrain where the other instance's volume lives.

*What happens to the data if the node or pod dies?* Pod dies, same node: the
StatefulSet-like PVC binding re-attaches the same volume, Postgres replays its
WAL, no data lost - this is the "raw StatefulSet" case, and I proved it (see
the Chaos section: two of these). Node dies, taking one instance's volume
with it: with the old single-instance StatefulSet this replaced, that was
unrecoverable data loss - one node was the entire cluster's storage. With
CNPG's 2-instance cluster, the surviving instance (on a different node, with
its own separate volume) is promoted and keeps serving; the lost node's data
is gone, but the cluster's data isn't - proved twice by force-killing the
primary under a live connection.

*How would you back it up and restore it?* CloudNativePG's `ScheduledBackup`
handles the schedule (continuous WAL archiving plus a nightly base backup to
MinIO); I didn't stop at configuring this and describing it, I ran it: I took
a backup, deleted the cluster it came from entirely, and restored a fresh
`Cluster` from that backup, then confirmed the exact row I'd written
beforehand was present. That full loop - and the WAL-archiving-timing bug it
surfaced - is in the Chaos and "real bugs" sections below.

**The bug this surfaced:** I originally provisioned CNPG on minikube's
default `standard` StorageClass (the legacy hostpath provisioner) and every
instance failed `initdb` with `Permission denied` writing to its own data
directory. Kubernetes explicitly exempts the HostPath volume type from
`fsGroup` enforcement - a real, documented limitation, not a misconfiguration
on my part - so a non-root container (which is exactly what CNPG's postgres
image is) can't be given group-write access to a `standard`-class volume by
Kubernetes' normal mechanism. `csi-hostpath-driver` is a real CSI
implementation and honors `fsGroup` correctly, which is the actual fix, not a
workaround around it.

## 3. What minikube did for me

Things minikube handed me for free that I'd otherwise have had to build - and
one thing it specifically did *not* hand me for free, which I only discovered
by hitting it:

- **Control-plane bootstrap.** `kube-apiserver`, `etcd`, `controller-manager`,
  `scheduler` all come up correctly wired (certs, RBAC bootstrap tokens,
  static pod manifests) with one command. On bare metal that's kubeadm plus
  getting the cert hierarchy and etcd cluster right by hand.
- **CNI install.** Even with `--cni=calico`, minikube handles fetching and
  applying the Calico manifests against the right pod CIDR for the cluster it
  just created.
- **Ingress load-balancing.** The `ingress` addon gives me a working
  ingress-nginx plus a path to the node's IP via `minikube tunnel`. On bare
  metal, "no cloud load balancer" means I'd need something like MetalLB
  handing out real LAN IPs via ARP/BGP first.
- **The storage provisioner - and its limits.** The default `standard`
  StorageClass makes `PersistentVolumeClaim` just work, until it doesn't:
  it's backed by plain hostPath, which Kubernetes exempts from `fsGroup`
  enforcement, which is exactly what broke CNPG's non-root Postgres container
  on first attempt (see the storage ADR above). minikube also ships a proper
  answer to this as an opt-in addon (`csi-hostpath-driver`), which is the
  right lesson: the convenient default and the production-shaped answer are
  two different addons, and I only found that out by hitting the failure, not
  by reading about it in advance. Bare metal has no default provisioner at
  all, so this exact class of bug - fsGroup vs. hostPath - is one I'd meet
  again there regardless of which CSI I picked.
- **etcd plus its backup.** minikube's etcd is a single instance with no
  backup job and no story for corruption. Bare metal self-managed Kubernetes
  means I own etcd's disk performance, its 3-or-5-node quorum, and a
  `etcdctl snapshot save` cron job going somewhere durable - losing etcd is
  losing the cluster's entire state, not just one workload's.

## 4. Production gaps

What's missing before this serves real traffic, roughly in the order I'd close
these gaps:

1. **The backup store itself has no HA.** CloudNativePG's Postgres cluster is
   now genuinely HA (proven below), but its backup target - one MinIO pod on
   one PVC - isn't. If that node dies at the same moment as a Postgres
   failure, the "restore from backup" safety net is also down. A real
   deployment would use managed object storage (S3, GCS) or a MinIO
   distributed deployment, not a single pod.
2. **No control-plane HA.** One control-plane node. A node reboot at the
   wrong moment takes the whole cluster's API server down with it, even
   though Postgres itself would survive. Real HA means a 3-node (or managed)
   control plane.
3. **Secrets backend is cluster-local, and secrets at rest in etcd aren't
   encrypted.** Two related but distinct gaps. First: Sealed Secrets' private
   key lives only in this cluster - a real secret backend (Vault, cloud KMS +
   External Secrets Operator) decouples secret material from any one
   cluster's lifecycle and adds audit logging on every read, which Sealed
   Secrets doesn't give you. Second, and more fundamental: once a
   `SealedSecret` is decrypted into a live `Secret`, that object sits in etcd
   in plaintext by default - confirmed on this specific cluster, not assumed
   (see the security ADR and bug log above). Closing this needs an
   `EncryptionConfiguration` on the apiserver, ideally KMS-backed envelope
   encryption rather than the weaker built-in providers. I verified the fix
   is mechanically straightforward but chose not to apply it live to this
   already-built cluster given the operational risk of editing a running
   apiserver's static pod manifest this late - a judgment call, not an
   oversight, and the first thing I'd do differently starting fresh (bake
   `--encryption-provider-config` into cluster bring-up from the start,
   rather than retrofit it). Same reasoning, same fix category: Kubernetes
   API audit logging is also off on this cluster (confirmed by checking the
   apiserver's static pod manifest directly, not assumed) - no record of
   who did what against the API server. Both are apiserver-flag changes I'd
   bake in at cluster bring-up on a real deployment rather than retrofit
   onto a running one.
4. **No upgrade story.** I pinned Kubernetes, Calico, ArgoCD, CloudNativePG,
   and every image by tag or digest, but there's no tested path for rolling
   any of those forward - no staging cluster to validate a Kubernetes minor
   bump against, no documented rollback if a CNPG upgrade breaks a CRD version.
5. **Single cluster, single region.** No multi-cluster failover, no story for
   what happens if the one cluster's underlying host/region has an outage.
6. **Observability is minimal by design.** One Prometheus, one alert, no
   Alertmanager routing to a real paging system, no long-term metrics storage
   (retention is 6h on `emptyDir`), no logs pipeline at all, and nothing yet
   watching CNPG's own failover events specifically (see Runbook - this is
   the most concrete near-term fix). Fine for this exercise; not fine for
   anything with an on-call rotation.
7. **Supply chain is unverified, and I have real numbers on why that
   matters, not just the theoretical concern.** Images are pinned by
   tag/digest but nothing stops an unsigned or untrusted image from running -
   no admission policy, no image signing/verification (cosign +
   Kyverno/OPA Gatekeeper would close that half of it). The other half is
   what's actually *in* the pinned images: I ran `trivy image` against all
   four I control the base image for and got real CRITICAL/HIGH counts, not
   a guess - `qoves-api` (python:3.12.4-slim-bookworm base): 12 critical, 69
   high; MinIO: 4 critical, 85 high; Prometheus: 6 critical, 80 high; CNPG's
   Postgres image: 34 critical, 233 high. Pinning a digest guarantees
   *reproducibility*, not *safety* - a digest-pinned image is exactly as
   vulnerable today as it was the day I pinned it, and none of these numbers
   go down until I bump the tag. I didn't change `app/Dockerfile`'s base
   image - it's the assignment's exact provided file and the app itself
   isn't scored - but for the three images that are my own choice, this is
   the concrete argument for a scheduled base-image bump, not just "keep
   pins fresh" as an abstract idea. A real deployment wires `trivy` (or
   equivalent) into CI as a merge gate, not a one-off manual check like this.

## 5. Chaos: killing the primary, twice, under a live connection

I force-killed the CNPG primary pod (`kubectl delete pod --grace-period=0
--force`) twice while a client inside the cluster ran a tight loop of
`SELECT pg_is_in_recovery()` against the `postgres-rw` service - the same
service the API connects to.

**What I saw:** in both runs, the query loop hit one long stall - roughly
28-31 seconds in the run I have exact timestamps for - then resumed
immediately and kept returning correct answers (`f`, meaning "this is the
primary") against whichever pod CNPG had just promoted. `kubectl get cluster
postgres` confirmed the promotion each time (`PRIMARY` field flipped to the
surviving instance), and the killed pod came back a few seconds later as a
fresh replica, resynced automatically. Data written before either kill
(a `restart_proof` table) was still there and correct after both.

**Why it took ~28-30 seconds, and why that number matters more than it looks:**
that window isn't CNPG's actual promotion time - the operator detects primary
failure and re-labels the new primary considerably faster than that. Most of
the delay is the client's TCP connection attempt to the *old* pod's IP hanging
until the OS-level connection attempt gave up, because my test script didn't
set an explicit `connect_timeout` on the `psql` connection. That's a real
lesson, not a footnote: a production client library needs a short, deliberate
connection timeout (a few seconds, not the OS default), or the *application-
visible* outage during a failover will be dominated by how long a dead
TCP connection takes to be recognized as dead, not by how fast the database
actually recovered. CNPG did its job quickly; a naive client made the outage
look much longer than it was.

**What I'd change with more time:** rerun this with `psql`'s `connect_timeout=2`
set explicitly and confirm the app-visible window shrinks to something close
to CNPG's actual promotion time, and add a Prometheus alert on
`cnpg_pg_replication_in_recovery` transitions (or simply on the Cluster's
`status.phase`) so a failover shows up as a page, not just something you find
by checking `kubectl get cluster` after the fact - this is the concrete next
step for the observability gap listed above.

## 6. Real bugs I hit and fixed (not hypothetical)

Every one of these was a genuine failure I hit while building this, not a
contrived example:

1. **`pg_isready -U postgres` against a cluster with no `postgres` role.**
   Setting `POSTGRES_USER=appuser` makes the official Postgres image create
   `appuser` as the superuser and skip creating a `postgres` role entirely.
   The probe was checking a role that didn't exist. Fixed to
   `-U appuser -d appdb`. (This was in the earlier raw-StatefulSet version;
   CNPG manages its own probes now, but the underlying lesson - know which
   role your own bootstrap actually creates - is the same one.)
2. **Prometheus RBAC broader than the scrape config needed.** A `ClusterRole`
   granting cluster-wide read on nodes/services/endpoints/pods, when the
   scrape config only ever discovers pods in the `app` namespace. Scoped down
   to a `Role`+`RoleBinding` in `app` only.
3. **ArgoCD self-heal fighting the HPA.** Covered in the scaling ADR above -
   `ignoreDifferences` on `/spec/replicas` was the fix.
4. **CNPG's `imageName` rejected a bare digest.** `spec.imageName:
   "...@sha256:..."` fails validation with "can't detect upgrades." CNPG
   needs the tag alongside the digest (`image:tag@sha256:digest`) specifically
   so it can compare versions across reconciles - a bare digest pin, which is
   what I use everywhere else in this repo, isn't enough for a resource whose
   controller needs to reason about upgrades.
5. **Egress NetworkPolicy is not implied by an ingress NetworkPolicy on the
   other side, ever.** The MinIO bucket-creation Job timed out reaching MinIO
   even though MinIO had an explicit ingress-allow for it - because
   default-deny also blocks the *job's own egress*, and I'd only written the
   destination-side rule. Both sides of every path need their own explicit
   allow; I'd already gotten this right for api↔postgres and had to
   relearn it for the bucket job.
6. **`ipBlock` to the Kubernetes Service ClusterIP silently doesn't work for
   the apiserver.** I allowed CNPG's egress to `10.96.0.1/32:443` (the
   `kubernetes` Service's ClusterIP) and it timed out. minikube's
   `kube-apiserver` runs as a host-networked static pod, so kube-proxy's DNAT
   rewrites the destination to the control-plane node's real IP
   (`192.168.49.2:8443` on this profile) in the `nat` table before Calico's
   `filter`-table policy check ever evaluates the packet - meaning an
   `ipBlock` keyed to the ClusterIP never matches what Calico actually sees.
   The fix was targeting the real post-DNAT endpoint. This is minikube-
   specific and not portable (a real cluster fronts the apiserver with a
   stable VIP/LB), which I say explicitly in the manifest's comment so it
   doesn't get copy-pasted somewhere it'll silently fail again.
7. **`fsGroup` doesn't apply to HostPath volumes.** Covered in the storage
   ADR above - CNPG's initdb got `Permission denied` on minikube's default
   StorageClass; the fix was `csi-hostpath-driver`, a real CSI implementation,
   not a permissions workaround on the container.
8. **A tiny transaction can race WAL archiving.** My first on-demand `Backup`
   completed successfully but restoring from it produced a database missing a
   table I'd created moments earlier. `pg_stat_archiver` showed archiving was
   healthy - the actual cause was that my transaction's WAL record was still
   sitting in the *current*, not-yet-rotated WAL segment when the backup ran,
   so restore replayed only up to the last archived segment. Forcing
   `CHECKPOINT; SELECT pg_switch_wal();` before triggering the backup fixed
   it. The lesson: an on-demand backup taken immediately after a write isn't
   automatically consistent with that write unless something forces the WAL
   segment closed first - continuous archiving on a schedule usually papers
   over this in practice, but a backup-then-verify script that doesn't
   account for it will look correct until the one time it isn't.
9. **`ScheduledBackup`'s cron format has an extra field, and I didn't notice
   until I checked what actually ran.** I wrote `schedule: "0 2 * * *"`
   intending "2am daily," which is correct Kubernetes CronJob syntax. CNPG's
   `ScheduledBackup` uses `robfig/cron` format instead, which prepends a
   seconds field - so a 5-field schedule doesn't mean what it looks like it
   means. My schedule was actually parsed as `sec=0 min=2 hour=* ...`, i.e.
   "every hour at :02:00," not "once a day." I found this by noticing an
   unexpected `Backup` object named `postgres-nightly-20260729170200` (17:02,
   not 02:00) sitting in the namespace during this audit, not by re-reading
   the docs - the docs do state the format difference explicitly, I just
   hadn't internalized it when I first wrote the schedule. Fixed to the
   correct 6-field `"0 0 2 * * *"`. The general lesson: a schedule string
   that's syntactically valid and produces output that looks plausible (it
   did run, it did complete, the backup was real) is not the same as a
   schedule string that does what you meant - checking one real firing
   against the wall clock caught this; nothing about the resource's own
   status fields would have.
10. **MinIO was running as root, and nothing had told me.** I'd verified
    MinIO functionally (backups worked, restore worked) and never checked
    what user it actually ran as. It was `uid=0`. Neither the API nor MinIO
    Deployment had any `securityContext` at all - the API only avoided this
    because the app image's Dockerfile happens to set a `USER` directive,
    which the cluster itself was doing nothing to enforce. Fixed by adding
    explicit `securityContext` to both plus the bucket-creation Job (see the
    security ADR above), which is exactly the kind of gap "it works" doesn't
    catch and a deliberate security pass does.
11. **Recreating a PVC with a different (immutable) `storageClassName`
    wedged ArgoCD's sync state, and the fix wasn't obvious.** After deleting
    the old `minio-data` PVC to let it recreate on `csi-hostpath-sc`, ArgoCD's
    sync got stuck reporting "Detected changes to resource minio-data which
    is currently being deleted" indefinitely - even after the PVC was
    actually gone, even after restarting both `argocd-application-controller`
    and `argocd-redis` to clear any cached live-state view. The operation
    itself was wedged in the `Application`'s own `.status.operationState`
    (an automated sync from an older revision, started before my delete,
    that never resolved). Neither clearing `.spec.operation` nor a fresh
    sync request unstuck it - what worked was deleting the `Application`
    object itself (safe here: no cascade finalizer on child apps, only on
    `root`) and letting the parent app-of-apps recreate it fresh. The
    lesson: an immutable-field change on a resource ArgoCD manages needs the
    old object gone *before* the new sync attempt starts, not deleted
    mid-flight while a previous operation is still resolving against it -
    and when ArgoCD's own operation state wedges, the reliable fix is
    recreating the `Application`, not chasing its cache.
12. **`gunicorn` crashes immediately under `readOnlyRootFilesystem` with no
    writable `/tmp`.** After the assignment brief was updated to specify the
    exact app (Flask + gunicorn, replacing an app I'd written myself when no
    template existed), I re-applied the same hardening pass to it and hit a
    real crash: gunicorn's worker heartbeat mechanism (`WorkerTmp`) calls
    `tempfile.mkstemp()` against `/tmp` on every worker boot, and with no
    writable filesystem anywhere, that raises `FileNotFoundError: No usable
    temporary directory found` before the process ever binds a port. I found
    this by testing the exact container under `docker run --read-only`
    locally before deploying, not by watching it fail in the cluster. Fixed
    with a mounted `emptyDir` at `/tmp` - keeps the rest of the filesystem
    read-only, gives gunicorn the one path it actually needs.
13. **A CNI plugin can wedge on one node after a host sleep/restart, while
    workloads already running on that node stay completely unaffected.**
    After a Colima restart (see bug list context above), new pod creation on
    one node started failing with `plugin type="calico" failed (add): error
    getting ClusterInformation: connection is unauthorized` - but the pods
    already running on that same node (Postgres, one API replica) kept
    serving traffic the entire time, because an existing pod's network
    sandbox doesn't need the CNI ADD path again. This is a sharp distinction
    worth having internalized rather than assumed: "workloads on this node
    are healthy" is not evidence that "this node can schedule new pods
    correctly." The fix was deleting and letting the `calico-node` DaemonSet
    pod on that node restart, which cleared whatever stale connection state
    it was holding.
14. **My own security hardening pass missed a workload, because it lives in
    a different namespace than the one I was focused on.** I'd hardened
    every workload's `securityContext` and rolled out PSS enforcement for
    the `app` namespace, and considered the pass complete. A later,
    dedicated "check everything again" review caught that Prometheus - in
    the `monitoring` namespace, not `app` - had no `securityContext` at all,
    and `monitoring` had no PSS labels whatsoever. It was already running
    non-root (`uid 65534`, the image's own default), so nothing was actively
    broken, but nothing at the cluster layer would have caught it if that
    default ever changed - the exact same class of gap I'd already found
    and fixed for the API and MinIO, just in a namespace I hadn't
    re-checked. Fixed the same way: tested `readOnlyRootFilesystem` locally
    first (found it needs `fsGroup` set for its `emptyDir` to be writable
    by the non-root UID, confirmed by reproducing the failure with `docker
    run` before touching the cluster), then rolled `monitoring` through the
    same safe audit+warn-then-enforce PSS sequence, verified via a real pod
    recreation each time. The lesson: a security pass scoped to "the
    namespace I've been working in" isn't a security pass over the whole
    cluster - worth explicitly re-checking every namespace, not just the
    one most recently touched.

**Not a bug I fixed, but worth understanding since it's now code I have to
defend too:** the provided app runs gunicorn with `--workers 2`, and
`prometheus_client`'s default `Counter` registry is per-process. Gunicorn
workers are separate OS processes with separate memory, and nothing in
`main.py` wires up `prometheus_client`'s `multiprocess` module (the standard
fix, which needs a shared `PROMETHEUS_MULTIPROC_DIR` and a different
`generate_latest()` call). So `http_requests_total` in `/metrics` reflects
whichever single worker process happened to handle that particular scrape
request, not a true count across both workers - or across the 2 pod
replicas, for that matter. I didn't try to force a clean live demonstration
of this (routing a specific request to a specific worker from outside the
pod isn't something curl gives you clean control over, and a flaky demo is
worse than none), but it's well-documented `prometheus_client` behavior, not
a guess. It doesn't affect my `APITargetDown` alert, which only depends on
`up{job="api"}` - the scrape succeeding at all, not the accuracy of any
metric inside it. I didn't touch `main.py` to fix this: it's the assignment's
own provided file, verbatim, and "the app itself is not scored" - but I'd
raise it unprompted in a walkthrough, because understanding a limitation in
code I didn't write is exactly what "defend every line" is asking for.

## 7. Runbook: the Postgres primary dies

This replaces the version of this runbook I'd have written against the raw
StatefulSet, because the actual failure mode - and the correct response to
it - is now different, and I verified this version against a real kill, not
just against the CRD docs.

1. **Detect.** `kubectl -n app get cluster postgres` shows `STATUS` other
   than "Cluster in healthy state" and a `PRIMARY` field mid-transition, or
   the API's `/healthz` starts intermittently 503ing. Today this requires
   someone to check; the concrete near-term fix (Production gaps #6) is
   alerting directly on CNPG's own status/metrics instead of only inferring
   it from the API.
2. **Do nothing manually, first.** This is the change from the old runbook:
   CNPG's controller detects the failed primary and promotes the healthy
   standby automatically - I proved this twice under test, with zero data
   loss both times and no human action required. Confirm it happened:
   ```bash
   kubectl -n app get cluster postgres
   kubectl -n app get pods -l cnpg.io/cluster=postgres
   ```
   Wait for `STATUS: Cluster in healthy state` before doing anything else.
3. **Expect a real but bounded interruption, not zero downtime.** Per the
   chaos test above, the client-visible gap was ~28-30 seconds, mostly client
   TCP timeout behavior rather than CNPG's own promotion time. If that
   duration matters for an SLA, the fix is a shorter client-side
   `connect_timeout`, not a change to CNPG's failover logic.
4. **If the failed pod doesn't rejoin as a replica within a few minutes,**
   check its PVC:
   ```bash
   kubectl -n app describe pod <failed-instance>
   kubectl -n app get pvc
   ```
   distinguish "node came back, PVC re-attached, resyncing" (normal, just
   slow) from "node/PVC actually gone" (this is where the single-node
   `csi-hostpath-sc` volume's limits matter - see the storage ADR).
5. **If both instances are genuinely gone** (not just the primary), this is a
   real data-loss event unless backups exist - which, as of this build, they
   do. Recovery is a `Cluster` with `spec.bootstrap.recovery.backup.name`
   pointing at the latest completed `Backup`, exactly as I proved works in
   the Chaos section: create it, wait for `Cluster in healthy state`, verify
   the expected data is present, then point the API's `DATABASE_URL` at the
   new cluster's `-rw` service. All of this is git-defined except the
   emergency `Backup`/`Cluster` recovery objects themselves, which are
   one-shot operational actions, not desired state - the `ScheduledBackup`
   that produces the backups they'd restore from is the part that's
   git-managed and always was.
6. **Postmortem action:** the actual fix isn't "we recovered manually" - it's
   closing Production gap #1 (MinIO itself has no HA) and #6 (alert directly
   on CNPG status), so the next incident is a page with a known-good runbook
   attached, not a rediscovery of this one.
