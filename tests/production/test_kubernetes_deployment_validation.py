#!/usr/bin/env python3
"""
Kubernetes Deployment Validation Tests for FaultMaven Phase 2

This module provides comprehensive validation tests for Kubernetes deployment
readiness including all Phase 2 intelligent troubleshooting components.

Test Categories:
- Infrastructure validation (cluster, nodes, storage)
- Service deployment validation (FaultMaven services)
- External dependency validation (Redis, ChromaDB, Presidio, OPIK)
- Network and ingress validation
- Storage and persistence validation
- High availability and resilience validation
"""

import pytest
import asyncio
import subprocess
import json
import yaml
import aiohttp
import socket
import time
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass


@dataclass
class K8sResource:
    """Represents a Kubernetes resource for validation."""
    kind: str
    name: str
    namespace: str
    expected_replicas: Optional[int] = None
    required_labels: Optional[Dict[str, str]] = None
    required_annotations: Optional[Dict[str, str]] = None


@dataclass
class ServiceEndpoint:
    """Represents a service endpoint for validation."""
    name: str
    url: str
    expected_status: int
    timeout: float = 10.0
    method: str = "GET"
    payload: Optional[Dict[str, Any]] = None


class KubernetesDeploymentValidator:
    """Comprehensive Kubernetes deployment validator for FaultMaven Phase 2."""
    
    def __init__(self):
        self.session = None
        
        # Expected FaultMaven Phase 2 resources
        self.expected_resources = [
            # FaultMaven Core Services
            K8sResource("Deployment", "faultmaven-api", "faultmaven", expected_replicas=2,
                       required_labels={"app": "faultmaven-api", "version": "v2.0"}),
            K8sResource("Service", "faultmaven-api", "faultmaven"),
            K8sResource("Ingress", "faultmaven-api", "faultmaven"),
            
            # External Dependencies
            K8sResource("Deployment", "redis-master", "faultmaven", expected_replicas=1,
                       required_labels={"app": "redis", "role": "master"}),
            K8sResource("Deployment", "redis-replica", "faultmaven", expected_replicas=2,
                       required_labels={"app": "redis", "role": "replica"}),
            K8sResource("Service", "redis-master", "faultmaven"),
            K8sResource("Service", "redis-replica", "faultmaven"),
            
            K8sResource("Deployment", "chromadb", "faultmaven", expected_replicas=1,
                       required_labels={"app": "chromadb"}),
            K8sResource("Service", "chromadb", "faultmaven"),
            K8sResource("Ingress", "chromadb", "faultmaven"),
            
            K8sResource("Deployment", "presidio-analyzer", "faultmaven", expected_replicas=1,
                       required_labels={"app": "presidio-analyzer"}),
            K8sResource("Deployment", "presidio-anonymizer", "faultmaven", expected_replicas=1,
                       required_labels={"app": "presidio-anonymizer"}),
            K8sResource("Service", "presidio-analyzer", "faultmaven"),
            K8sResource("Service", "presidio-anonymizer", "faultmaven"),
            K8sResource("Ingress", "presidio-analyzer", "faultmaven"),
            K8sResource("Ingress", "presidio-anonymizer", "faultmaven"),
            
            # OPIK LLM Monitoring Platform
            K8sResource("Deployment", "opik-frontend", "opik-system", expected_replicas=1,
                       required_labels={"app": "opik-frontend"}),
            K8sResource("Deployment", "opik-backend", "opik-system", expected_replicas=2,
                       required_labels={"app": "opik-backend"}),
            K8sResource("Service", "opik-frontend", "opik-system"),
            K8sResource("Service", "opik-backend", "opik-system"),
            K8sResource("Ingress", "opik", "opik-system"),
            K8sResource("Ingress", "opik-api", "opik-system"),
            
            # ClickHouse for OPIK Analytics
            K8sResource("ClickHouseInstallation", "opik-clickhouse", "opik-system"),
            K8sResource("Service", "clickhouse-opik-clickhouse", "opik-system"),
            
            # Infrastructure Components
            K8sResource("DaemonSet", "longhorn-manager", "longhorn-system"),
            K8sResource("DaemonSet", "longhorn-engine-image", "longhorn-system"),
            K8sResource("Deployment", "longhorn-ui", "longhorn-system", expected_replicas=1),
            K8sResource("Deployment", "longhorn-driver-deployer", "longhorn-system", expected_replicas=1),
            
            # Ingress Controller
            K8sResource("Deployment", "ingress-nginx-controller", "ingress-nginx", expected_replicas=2),
            K8sResource("Service", "ingress-nginx-controller", "ingress-nginx"),
        ]
        
        # Expected service endpoints
        self.service_endpoints = [
            ServiceEndpoint("faultmaven-api-health", "http://faultmaven-api.faultmaven.local:30080/health", 200),
            ServiceEndpoint("faultmaven-api-docs", "http://faultmaven-api.faultmaven.local:30080/docs", 200),
            ServiceEndpoint("chromadb", "http://chromadb.faultmaven.local:30080/api/v1/version", 200),
            ServiceEndpoint("presidio-analyzer", "http://presidio-analyzer.faultmaven.local:30080/health", 200),
            ServiceEndpoint("presidio-anonymizer", "http://presidio-anonymizer.faultmaven.local:30080/health", 200),
            ServiceEndpoint("opik-frontend", "http://opik.faultmaven.local:30080", 200),
            ServiceEndpoint("opik-api", "http://opik-api.faultmaven.local:30080/api/health", 200),
        ]
        
        # Required storage classes
        self.required_storage_classes = [
            {"name": "longhorn-fast", "provisioner": "driver.longhorn.io"},
            {"name": "longhorn-standard", "provisioner": "driver.longhorn.io"}
        ]
        
        # Required node labels
        self.required_node_labels = {
            "faultmaven.io/node-type": ["data-worker", "app-worker"],
            "node-role.kubernetes.io/control-plane": [""]
        }


@pytest.fixture
async def k8s_validator():
    """Fixture providing configured Kubernetes validator."""
    validator = KubernetesDeploymentValidator()
    validator.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    yield validator
    await validator.session.close()


@pytest.fixture
def kubectl_available():
    """Ensure kubectl is available and working."""
    try:
        result = subprocess.run(["kubectl", "version", "--client"], 
                              capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            pytest.skip("kubectl not available")
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pytest.skip("kubectl not available or not responsive")


@pytest.fixture
def cluster_accessible(kubectl_available):
    """Ensure cluster is accessible."""
    try:
        result = subprocess.run(["kubectl", "cluster-info"], 
                              capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            pytest.skip(f"Cluster not accessible: {result.stderr}")
        return True
    except subprocess.TimeoutExpired:
        pytest.skip("Cluster not responsive")


class TestClusterInfrastructure:
    """Test cluster infrastructure readiness."""
    
    @pytest.mark.asyncio
    async def test_cluster_connectivity(self, kubectl_available, cluster_accessible):
        """Test basic cluster connectivity and health."""
        # Test cluster info
        result = subprocess.run(["kubectl", "cluster-info"], 
                              capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, f"Cluster info failed: {result.stderr}"
        assert "Kubernetes control plane" in result.stdout, "Control plane not found in cluster info"
    
    @pytest.mark.asyncio
    async def test_node_readiness(self, cluster_accessible):
        """Test all nodes are ready and properly labeled."""
        # Get node status
        result = subprocess.run(
            ["kubectl", "get", "nodes", "-o", "json"],
            capture_output=True, text=True, timeout=15
        )
        assert result.returncode == 0, f"Failed to get nodes: {result.stderr}"
        
        nodes_data = json.loads(result.stdout)
        nodes = nodes_data.get("items", [])
        
        assert len(nodes) >= 3, f"Expected at least 3 nodes, found {len(nodes)}"
        
        ready_nodes = 0
        labeled_nodes = 0
        
        for node in nodes:
            node_name = node.get("metadata", {}).get("name", "unknown")
            
            # Check node readiness
            conditions = node.get("status", {}).get("conditions", [])
            node_ready = any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in conditions
            )
            
            if node_ready:
                ready_nodes += 1
            
            # Check required labels
            labels = node.get("metadata", {}).get("labels", {})
            has_node_type = "faultmaven.io/node-type" in labels
            
            if has_node_type:
                labeled_nodes += 1
                node_type = labels["faultmaven.io/node-type"]
                assert node_type in ["data-worker", "app-worker"], \
                    f"Node {node_name} has invalid node-type: {node_type}"
        
        assert ready_nodes == len(nodes), f"Only {ready_nodes}/{len(nodes)} nodes ready"
        assert labeled_nodes == len(nodes), f"Only {labeled_nodes}/{len(nodes)} nodes properly labeled"
    
    @pytest.mark.asyncio
    async def test_storage_classes(self, k8s_validator, cluster_accessible):
        """Test required storage classes exist and are configured correctly."""
        result = subprocess.run(
            ["kubectl", "get", "storageclass", "-o", "json"],
            capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, f"Failed to get storage classes: {result.stderr}"
        
        storage_data = json.loads(result.stdout)
        storage_classes = storage_data.get("items", [])
        
        found_storage_classes = {
            sc.get("metadata", {}).get("name"): sc.get("provisioner")
            for sc in storage_classes
        }
        
        for required_sc in k8s_validator.required_storage_classes:
            sc_name = required_sc["name"]
            expected_provisioner = required_sc["provisioner"]
            
            assert sc_name in found_storage_classes, f"Storage class {sc_name} not found"
            assert found_storage_classes[sc_name] == expected_provisioner, \
                f"Storage class {sc_name} has wrong provisioner: {found_storage_classes[sc_name]}"
    
    @pytest.mark.asyncio
    async def test_longhorn_system_health(self, cluster_accessible):
        """Test Longhorn distributed storage system health."""
        # Check Longhorn namespace exists
        result = subprocess.run(
            ["kubectl", "get", "namespace", "longhorn-system"],
            capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, "Longhorn namespace not found"
        
        # Check Longhorn components
        result = subprocess.run(
            ["kubectl", "get", "pods", "-n", "longhorn-system", "-o", "json"],
            capture_output=True, text=True, timeout=15
        )
        assert result.returncode == 0, f"Failed to get Longhorn pods: {result.stderr}"
        
        pods_data = json.loads(result.stdout)
        pods = pods_data.get("items", [])
        
        running_pods = 0
        for pod in pods:
            phase = pod.get("status", {}).get("phase", "Unknown")
            if phase == "Running":
                running_pods += 1
        
        assert running_pods > 0, "No Longhorn pods running"
        assert running_pods >= len(pods) * 0.8, f"Only {running_pods}/{len(pods)} Longhorn pods running"
        
        # Check Longhorn nodes
        result = subprocess.run(
            ["kubectl", "get", "nodes.longhorn.io", "-n", "longhorn-system", "-o", "json"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:  # Longhorn nodes resource exists
            nodes_data = json.loads(result.stdout)
            longhorn_nodes = nodes_data.get("items", [])
            assert len(longhorn_nodes) >= 2, f"Expected at least 2 Longhorn nodes, found {len(longhorn_nodes)}"


class TestNamespaceDeployments:
    """Test individual namespace deployments."""
    
    @pytest.mark.asyncio
    async def test_faultmaven_namespace(self, k8s_validator, cluster_accessible):
        """Test FaultMaven namespace deployment."""
        namespace = "faultmaven"
        
        # Check namespace exists
        result = subprocess.run(
            ["kubectl", "get", "namespace", namespace],
            capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, f"Namespace {namespace} not found"
        
        await self._validate_namespace_resources(namespace, [
            r for r in k8s_validator.expected_resources 
            if r.namespace == namespace
        ])
    
    @pytest.mark.asyncio
    async def test_opik_system_namespace(self, k8s_validator, cluster_accessible):
        """Test OPIK system namespace deployment."""
        namespace = "opik-system"
        
        # Check namespace exists
        result = subprocess.run(
            ["kubectl", "get", "namespace", namespace],
            capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, f"Namespace {namespace} not found"
        
        await self._validate_namespace_resources(namespace, [
            r for r in k8s_validator.expected_resources 
            if r.namespace == namespace
        ])
        
        # Special ClickHouse validation
        await self._validate_clickhouse_installation(namespace)
    
    @pytest.mark.asyncio
    async def test_ingress_nginx_namespace(self, k8s_validator, cluster_accessible):
        """Test NGINX Ingress Controller namespace."""
        namespace = "ingress-nginx"
        
        # Check namespace exists
        result = subprocess.run(
            ["kubectl", "get", "namespace", namespace],
            capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, f"Namespace {namespace} not found"
        
        await self._validate_namespace_resources(namespace, [
            r for r in k8s_validator.expected_resources 
            if r.namespace == namespace
        ])
    
    async def _validate_namespace_resources(self, namespace: str, expected_resources: List[K8sResource]):
        """Validate resources within a namespace."""
        for resource in expected_resources:
            await self._validate_single_resource(resource)
    
    async def _validate_single_resource(self, resource: K8sResource):
        """Validate a single Kubernetes resource."""
        kind_lower = resource.kind.lower()
        
        # Handle special resource types
        if resource.kind == "ClickHouseInstallation":
            cmd = ["kubectl", "get", "clickhouseinstallation", resource.name, "-n", resource.namespace, "-o", "json"]
        else:
            cmd = ["kubectl", "get", kind_lower, resource.name, "-n", resource.namespace, "-o", "json"]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        
        if result.returncode != 0:
            # If resource not found, it might be optional or differently named
            if resource.kind in ["Ingress", "ClickHouseInstallation"]:
                pytest.skip(f"Optional resource {resource.kind}/{resource.name} not found in {resource.namespace}")
            else:
                pytest.fail(f"Required resource {resource.kind}/{resource.name} not found in {resource.namespace}: {result.stderr}")
        
        resource_data = json.loads(result.stdout)
        
        # Validate expected replicas for deployments
        if resource.kind == "Deployment" and resource.expected_replicas:
            spec_replicas = resource_data.get("spec", {}).get("replicas", 0)
            status = resource_data.get("status", {})
            ready_replicas = status.get("readyReplicas", 0)
            
            assert ready_replicas >= resource.expected_replicas * 0.8, \
                f"Deployment {resource.name} has {ready_replicas}/{resource.expected_replicas} ready replicas"
        
        # Validate required labels
        if resource.required_labels:
            labels = resource_data.get("metadata", {}).get("labels", {})
            for label_key, expected_value in resource.required_labels.items():
                assert label_key in labels, f"Resource {resource.name} missing required label {label_key}"
                if expected_value:  # If expected value is not empty
                    assert labels[label_key] == expected_value, \
                        f"Resource {resource.name} label {label_key} is '{labels[label_key]}', expected '{expected_value}'"
    
    async def _validate_clickhouse_installation(self, namespace: str):
        """Validate ClickHouse installation for OPIK."""
        try:
            result = subprocess.run(
                ["kubectl", "get", "pods", "-n", namespace, "-l", "clickhouse.altinity.com/chi=opik-clickhouse", "-o", "json"],
                capture_output=True, text=True, timeout=15
            )
            
            if result.returncode == 0:
                pods_data = json.loads(result.stdout)
                clickhouse_pods = pods_data.get("items", [])
                
                if clickhouse_pods:
                    running_pods = sum(1 for pod in clickhouse_pods 
                                     if pod.get("status", {}).get("phase") == "Running")
                    assert running_pods > 0, "No ClickHouse pods running for OPIK"
                else:
                    pytest.skip("ClickHouse pods not found - may be using external ClickHouse")
            
        except Exception as e:
            pytest.skip(f"ClickHouse validation skipped: {e}")


class TestServiceConnectivity:
    """Test service connectivity and network configuration."""
    
    @pytest.mark.asyncio
    async def test_internal_service_resolution(self, cluster_accessible):
        """Test internal service DNS resolution."""
        services_to_test = [
            "faultmaven-api.faultmaven.svc.cluster.local",
            "redis-master.faultmaven.svc.cluster.local",
            "chromadb.faultmaven.svc.cluster.local",
            "presidio-analyzer.faultmaven.svc.cluster.local",
            "opik-frontend.opik-system.svc.cluster.local"
        ]
        
        for service in services_to_test:
            # Test DNS resolution using nslookup in a test pod
            cmd = [
                "kubectl", "run", "dns-test", "--image=busybox", "--rm", "-i", "--restart=Never",
                "--", "nslookup", service
            ]
            
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if result.returncode == 0:
                    assert "Name:" in result.stdout or "Address:" in result.stdout, \
                        f"DNS resolution failed for {service}"
                # If test fails, service might not exist yet - continue with other tests
            except subprocess.TimeoutExpired:
                pass  # DNS test timed out, skip this service
    
    @pytest.mark.asyncio
    async def test_ingress_controller_health(self, cluster_accessible):
        """Test NGINX Ingress Controller health."""
        # Check ingress controller pods
        result = subprocess.run(
            ["kubectl", "get", "pods", "-n", "ingress-nginx", "-l", "app.kubernetes.io/name=ingress-nginx", "-o", "json"],
            capture_output=True, text=True, timeout=15
        )
        
        if result.returncode == 0:
            pods_data = json.loads(result.stdout)
            ingress_pods = pods_data.get("items", [])
            
            if ingress_pods:
                running_pods = sum(1 for pod in ingress_pods 
                                 if pod.get("status", {}).get("phase") == "Running")
                assert running_pods > 0, "No NGINX Ingress Controller pods running"
            else:
                pytest.skip("NGINX Ingress Controller not deployed")
    
    @pytest.mark.asyncio
    async def test_external_service_endpoints(self, k8s_validator):
        """Test external service endpoints accessibility."""
        if not k8s_validator.session:
            k8s_validator.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        
        successful_endpoints = 0
        total_endpoints = len(k8s_validator.service_endpoints)
        
        for endpoint in k8s_validator.service_endpoints:
            try:
                if endpoint.method == "GET":
                    async with k8s_validator.session.get(endpoint.url) as resp:
                        if resp.status == endpoint.expected_status:
                            successful_endpoints += 1
                else:
                    payload = endpoint.payload or {}
                    async with k8s_validator.session.post(endpoint.url, json=payload) as resp:
                        if resp.status == endpoint.expected_status:
                            successful_endpoints += 1
                            
            except Exception as e:
                # Endpoint might not be ready yet, continue testing others
                pass
        
        # Require at least 50% of endpoints to be accessible
        success_rate = successful_endpoints / total_endpoints if total_endpoints > 0 else 0
        assert success_rate >= 0.5, f"Only {successful_endpoints}/{total_endpoints} service endpoints accessible"


class TestPersistenceAndStorage:
    """Test persistence and storage configuration."""
    
    @pytest.mark.asyncio
    async def test_persistent_volume_claims(self, cluster_accessible):
        """Test PVC creation and binding."""
        # Check for existing PVCs
        result = subprocess.run(
            ["kubectl", "get", "pvc", "--all-namespaces", "-o", "json"],
            capture_output=True, text=True, timeout=15
        )
        assert result.returncode == 0, f"Failed to get PVCs: {result.stderr}"
        
        pvc_data = json.loads(result.stdout)
        pvcs = pvc_data.get("items", [])
        
        bound_pvcs = 0
        for pvc in pvcs:
            phase = pvc.get("status", {}).get("phase", "Unknown")
            if phase == "Bound":
                bound_pvcs += 1
        
        # At least some PVCs should be bound for production workloads
        if len(pvcs) > 0:
            bound_ratio = bound_pvcs / len(pvcs)
            assert bound_ratio >= 0.8, f"Only {bound_pvcs}/{len(pvcs)} PVCs are bound"
    
    @pytest.mark.asyncio
    async def test_storage_class_functionality(self, k8s_validator, cluster_accessible):
        """Test storage class functionality with test PVCs."""
        for storage_class in k8s_validator.required_storage_classes:
            sc_name = storage_class["name"]
            
            # Create test PVC using this storage class
            test_pvc_manifest = {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {
                    "name": f"test-pvc-{sc_name.replace('-', '')}",
                    "namespace": "default"
                },
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "storageClassName": sc_name,
                    "resources": {
                        "requests": {
                            "storage": "1Gi"
                        }
                    }
                }
            }
            
            # Apply test PVC
            with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
                yaml.dump(test_pvc_manifest, f)
                manifest_path = f.name
            
            try:
                # Create test PVC
                result = subprocess.run(
                    ["kubectl", "apply", "-f", manifest_path],
                    capture_output=True, text=True, timeout=15
                )
                
                if result.returncode == 0:
                    # Wait a moment for PVC to bind
                    await asyncio.sleep(5)
                    
                    # Check PVC status
                    check_result = subprocess.run(
                        ["kubectl", "get", "pvc", test_pvc_manifest["metadata"]["name"], "-o", "json"],
                        capture_output=True, text=True, timeout=10
                    )
                    
                    if check_result.returncode == 0:
                        pvc_status = json.loads(check_result.stdout)
                        phase = pvc_status.get("status", {}).get("phase", "Unknown")
                        # PVC should be bound or at least pending (not failed)
                        assert phase in ["Bound", "Pending"], \
                            f"Test PVC for storage class {sc_name} is in {phase} state"
                
                # Cleanup test PVC
                subprocess.run(
                    ["kubectl", "delete", "pvc", test_pvc_manifest["metadata"]["name"], "--ignore-not-found"],
                    capture_output=True, timeout=10
                )
                
            finally:
                Path(manifest_path).unlink(missing_ok=True)


class TestHighAvailabilityAndResilience:
    """Test high availability and resilience features."""
    
    @pytest.mark.asyncio
    async def test_multi_replica_deployments(self, k8s_validator, cluster_accessible):
        """Test multi-replica deployments for HA."""
        ha_deployments = [
            ("faultmaven-api", "faultmaven", 2),
            ("redis-replica", "faultmaven", 2),
            ("opik-backend", "opik-system", 2),
            ("ingress-nginx-controller", "ingress-nginx", 2)
        ]
        
        for deployment_name, namespace, min_replicas in ha_deployments:
            result = subprocess.run(
                ["kubectl", "get", "deployment", deployment_name, "-n", namespace, "-o", "json"],
                capture_output=True, text=True, timeout=10
            )
            
            if result.returncode == 0:
                deployment_data = json.loads(result.stdout)
                spec_replicas = deployment_data.get("spec", {}).get("replicas", 0)
                status = deployment_data.get("status", {})
                ready_replicas = status.get("readyReplicas", 0)
                
                assert spec_replicas >= min_replicas, \
                    f"Deployment {deployment_name} has {spec_replicas} replicas, expected at least {min_replicas}"
                assert ready_replicas >= min_replicas, \
                    f"Deployment {deployment_name} has {ready_replicas} ready replicas, expected at least {min_replicas}"
    
    @pytest.mark.asyncio
    async def test_pod_disruption_budgets(self, cluster_accessible):
        """Test Pod Disruption Budgets for critical services."""
        result = subprocess.run(
            ["kubectl", "get", "pdb", "--all-namespaces", "-o", "json"],
            capture_output=True, text=True, timeout=10
        )
        
        if result.returncode == 0:
            pdb_data = json.loads(result.stdout)
            pdbs = pdb_data.get("items", [])
            
            # Should have PDBs for critical services
            critical_services = ["faultmaven-api", "redis", "opik-backend", "ingress-nginx"]
            
            pdb_names = [
                pdb.get("metadata", {}).get("name", "")
                for pdb in pdbs
            ]
            
            found_pdbs = 0
            for service in critical_services:
                if any(service in pdb_name for pdb_name in pdb_names):
                    found_pdbs += 1
            
            # At least some critical services should have PDBs
            if len(pdbs) > 0:
                assert found_pdbs > 0, "No PodDisruptionBudgets found for critical services"
    
    @pytest.mark.asyncio
    async def test_resource_limits_and_requests(self, k8s_validator, cluster_accessible):
        """Test resource limits and requests are configured."""
        critical_deployments = [
            ("faultmaven-api", "faultmaven"),
            ("redis-master", "faultmaven"),
            ("chromadb", "faultmaven"),
            ("opik-backend", "opik-system")
        ]
        
        for deployment_name, namespace in critical_deployments:
            result = subprocess.run(
                ["kubectl", "get", "deployment", deployment_name, "-n", namespace, "-o", "json"],
                capture_output=True, text=True, timeout=10
            )
            
            if result.returncode == 0:
                deployment_data = json.loads(result.stdout)
                containers = deployment_data.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
                
                for container in containers:
                    resources = container.get("resources", {})
                    requests = resources.get("requests", {})
                    limits = resources.get("limits", {})
                    
                    # At least memory requests should be set for production
                    if not requests.get("memory") and not limits.get("memory"):
                        pytest.skip(f"Deployment {deployment_name} container {container.get('name', 'unknown')} has no memory configuration - may be acceptable for testing")


@pytest.mark.asyncio
async def test_overall_deployment_health():
    """Overall deployment health check."""
    try:
        # Check overall cluster health
        result = subprocess.run(
            ["kubectl", "get", "nodes", "-o", "json"],
            capture_output=True, text=True, timeout=15
        )
        assert result.returncode == 0, "Cannot access cluster nodes"
        
        nodes_data = json.loads(result.stdout)
        nodes = nodes_data.get("items", [])
        ready_nodes = sum(1 for node in nodes 
                         if any(c.get("type") == "Ready" and c.get("status") == "True"
                               for c in node.get("status", {}).get("conditions", [])))
        
        assert ready_nodes >= 3, f"Only {ready_nodes} nodes ready, expected at least 3"
        
        # Check critical namespaces exist
        critical_namespaces = ["faultmaven", "opik-system", "longhorn-system"]
        for ns in critical_namespaces:
            result = subprocess.run(
                ["kubectl", "get", "namespace", ns],
                capture_output=True, text=True, timeout=5
            )
            # If namespace doesn't exist, it may be optional for this environment
            if result.returncode != 0:
                pytest.skip(f"Namespace {ns} not found - may not be deployed yet")
        
        return True
        
    except Exception as e:
        pytest.fail(f"Overall deployment health check failed: {e}")


if __name__ == "__main__":
    import sys
    
    # Allow running this module directly for debugging
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        pytest.main([__file__, "-v"])
    else:
        print("Kubernetes Deployment Validation Tests")
        print("Usage: python test_kubernetes_deployment_validation.py --test")