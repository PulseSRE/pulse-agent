# Tool Eval Prompts

Real-world user prompts mapped to expected tool calls. Used for evaluating agent tool selection quality.

**Total prompts:** 84
**Tools excluded from eval:** 8 (internal/meta)

## SRE (64 prompts)

| Prompt | Expected Tools | Description |
|--------|---------------|-------------|
| why are my pods crashing in production | `list_pods`, `describe_pod`, `get_pod_logs`, `get_events` | Crashloop diagnosis should gather pod list, describe, logs, events |
| what's wrong with my cluster | `get_cluster_operators`, `get_events`, `list_pods`, `get_firing_alerts` | Cluster health check should check operators, events, pods, alerts |
| show me pods with high restart counts | `top_pods_by_restarts` | High restarts has a dedicated tool |
| check if there are any OOM killed pods | `list_pods`, `describe_pod`, `get_events` | OOM investigation needs pod status and events |
| why is my deployment not rolling out | `describe_resource`, `get_events`, `list_pods` | Stuck rollout needs deployment describe, events, pod status |
| show me warning events in the default namespace | `get_events` | Direct event query |
| what changed in the last hour | `get_recent_changes` | Recent changes has a dedicated tool |
| show me the logs for pod nginx-abc in production | `get_pod_logs` | Direct log retrieval |
| search logs for error connection refused across all pods | `search_logs` | Cross-pod log search |
| list all pods in kube-system | `list_pods` | Direct pod listing |
| show me all deployments | `list_resources` | Generic resource listing |
| list all PVCs in the cluster | `list_resources` | PVC listing via generic list_resources |
| show me all ingresses | `list_ingresses` | Ingress listing has dedicated tool |
| list routes in production namespace | `list_routes` | Route listing has dedicated tool |
| show me HPAs across all namespaces | `list_hpas` | HPA listing |
| list all cronjobs | `list_cronjobs` | Cronjob listing |
| show me running jobs | `list_jobs` | Job listing |
| show me node status | `list_resources`, `get_node_metrics` | Node health check |
| which nodes have disk pressure | `list_resources`, `get_events` | Node condition check |
| drain node worker-2 for maintenance | `drain_node` | Node drain operation |
| cordon node worker-1 | `cordon_node` | Node cordon operation |
| uncordon node worker-1 | `uncordon_node` | Node uncordon operation |
| show me CPU usage across the cluster | `get_prometheus_query` | PromQL query for CPU metrics |
| what's the memory usage by namespace | `get_prometheus_query` | PromQL query for memory metrics |
| show me pod resource usage in production | `get_pod_metrics` | Pod metrics has dedicated tool |
| are there any firing alerts | `get_firing_alerts` | Alert check |
| show me node metrics | `get_node_metrics` | Node metrics has dedicated tool |
| what prometheus metrics are available for CPU | `discover_metrics` | Metric discovery |
| check resource recommendations for production namespace | `get_resource_recommendations` | Right-sizing recommendations |
| scale my-deployment to 5 replicas | `scale_deployment` | Scale operation |
| restart the nginx deployment in production | `restart_deployment` | Restart operation |
| delete pod nginx-abc-xyz in default namespace | `delete_pod` | Pod deletion |
| rollback my-deployment to the previous version | `rollback_deployment` | Deployment rollback |
| apply this yaml to create a configmap | `apply_yaml` | YAML apply operation |
| what version of OpenShift are we running | `get_cluster_version` | Cluster version check |
| show me cluster operator status | `get_cluster_operators` | Operator health check |
| list operator subscriptions | `list_operator_subscriptions` | OLM subscription listing |
| show me the configmap kube-proxy in kube-system | `get_configmap` | ConfigMap retrieval |
| check TLS certificates | `get_tls_certificates` | Certificate check |
| describe the kubernetes service in default namespace | `describe_service` | Service description |
| show me endpoint slices for my-service | `get_endpoint_slices` | Endpoint slice check |
| test connectivity from pod-a to pod-b on port 8080 | `test_connectivity` | Network connectivity test |
| create a default deny network policy for production | `create_network_policy` | Network policy creation |
| describe pod nginx-abc in production | `describe_pod` | Pod description |
| show me resource relationships for deployment nginx | `get_resource_relationships` | Resource relationship tree |
| run ls /tmp in pod nginx-abc | `exec_command` | Exec into pod |
| when will we run out of CPU quota in production | `forecast_quota_exhaustion` | Quota forecast |
| is my HPA thrashing | `analyze_hpa_thrashing` | HPA analysis |
| suggest a fix for this CrashLoopBackOff | `suggest_remediation` | Remediation suggestions |
| build a timeline of what happened in production in the last hour | `correlate_incident` | Incident timeline correlation |
| list ArgoCD applications | `get_argo_applications` | Argo app listing |
| show me drift from git for the payments app | `detect_gitops_drift` | GitOps drift detection |
| create a PR to fix the replica count | `propose_git_change` | Git PR proposal |
| show me the ArgoCD app details for payments | `get_argo_app_detail` | Argo app detail view |
| what's the source repo for the payments argo app | `get_argo_app_source` | Argo app source info |
| show me the sync diff for payments app | `get_argo_sync_diff` | Argo sync diff |
| create an ArgoCD application for my-app | `create_argo_application` | Argo app creation |
| install the GitOps operator | `install_gitops_operator` | GitOps operator installation |
| compare pods across all clusters | `fleet_list_pods` | Multi-cluster pod comparison |
| list all clusters in the fleet | `fleet_list_clusters` | Fleet cluster listing |
| show me alerts across all clusters | `fleet_get_alerts` | Fleet-wide alert check |
| compare deployments across clusters | `fleet_list_deployments`, `fleet_compare_resource` | Fleet deployment comparison |
| I found a security issue, hand this off to the security team | `request_security_scan` | SRE to security handoff |
| log that I restarted the nginx deployment for debugging | `record_audit_entry` | Audit log recording |

## SECURITY (9 prompts)

| Prompt | Expected Tools | Description |
|--------|---------------|-------------|
| scan RBAC for overly permissive roles | `scan_rbac_risks` | RBAC risk scan |
| check pod security across the cluster | `scan_pod_security` | Pod security scan |
| audit network policies | `scan_network_policies` | Network policy audit |
| scan for privileged containers | `scan_scc_usage`, `scan_sccs` | SCC/privilege scan |
| check for exposed secrets | `scan_secrets` | Secret exposure scan |
| scan container images for vulnerabilities | `scan_images` | Image vulnerability scan |
| give me a security summary of the cluster | `get_security_summary` | Overall security posture |
| list service account secrets in production | `list_service_account_secrets` | Service account secret listing |
| this security finding needs SRE investigation | `request_sre_investigation` | Security to SRE handoff |

## VIEW DESIGNER (11 prompts)

| Prompt | Expected Tools | Description |
|--------|---------------|-------------|
| create a dashboard for production namespace | `plan_dashboard`, `namespace_summary`, `get_prometheus_query`, `create_dashboard` | Full dashboard creation flow |
| build me a cluster overview dashboard | `plan_dashboard`, `cluster_metrics`, `get_prometheus_query`, `create_dashboard` | Cluster dashboard creation |
| show me my saved dashboards | `list_saved_views` | View listing |
| add a memory chart to my dashboard | `get_prometheus_query`, `add_widget_to_view` | Widget addition to existing view |
| remove the third widget from my dashboard | `update_view_widgets` | Widget removal |
| what metrics are available for network monitoring | `discover_metrics` | Metric discovery for dashboard building |
| undo the last change to my dashboard | `undo_view_change` | View undo operation |
| delete my old cluster dashboard | `delete_dashboard` | Dashboard deletion |
| clone my production dashboard for staging | `clone_dashboard` | Dashboard cloning |
| show me cluster KPI metrics | `cluster_metrics` | Cluster metric cards |
| give me a namespace summary for staging | `namespace_summary` | Namespace summary cards |

## Excluded from Eval

These tools are internal/meta and do not need user-facing eval prompts:

- `critique_view`
- `get_cluster_patterns`
- `get_current_user`
- `get_view_details`
- `get_view_versions`
- `set_current_user`
- `set_store`
- `verify_query`
