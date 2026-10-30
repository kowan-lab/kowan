import { lazy, Suspense } from "react";
import { FeatureGate } from "./lib/features";
import { Route, Routes } from "react-router";
import { Box, Spinner } from "@/ui/kit";
import { RequireAuth } from "./auth/AuthContext";
import Layout from "./components/Layout";

// Route pages are lazily loaded so each ships as its own chunk and heavy
// libraries (CodeMirror, xterm, recharts) only load on the pages that use them.
const Login = lazy(() => import("./pages/Login"));
const Setup = lazy(() => import("./pages/Setup"));
const Dashboard = lazy(() => import("./pages/Dashboard"));
const Hosts = lazy(() => import("./pages/Hosts"));
const HostDetail = lazy(() => import("./pages/HostDetail"));
const ContainerDetail = lazy(() => import("./pages/ContainerDetail"));
const Images = lazy(() => import("./pages/Images"));
const ImageDetail = lazy(() => import("./pages/ImageDetail"));
const Volumes = lazy(() => import("./pages/Volumes"));
const VolumeDetail = lazy(() => import("./pages/VolumeDetail"));
const VolumeExplorer = lazy(() => import("./pages/VolumeExplorer"));
const Networks = lazy(() => import("./pages/Networks"));
const NetworkMap = lazy(() => import("./pages/NetworkMap"));
const Exposure = lazy(() => import("./pages/Exposure"));
const NetworkDetail = lazy(() => import("./pages/NetworkDetail"));
const Connections = lazy(() => import("./pages/Connections"));
const Registries = lazy(() => import("./pages/Registries"));
const Secrets = lazy(() => import("./pages/Secrets"));
const Notifications = lazy(() => import("./pages/Notifications"));
const RegistryExplorer = lazy(() => import("./pages/RegistryExplorer"));
const Workloads = lazy(() => import("./pages/Workloads"));
const WorkloadDetail = lazy(() => import("./pages/WorkloadDetail"));
const DeploymentCreate = lazy(() => import("./pages/DeploymentCreate"));
const Templates = lazy(() => import("./pages/Templates"));
const Workflows = lazy(() => import("./pages/Workflows"));
const GitOps = lazy(() => import("./pages/GitOps"));
const Backups = lazy(() => import("./pages/Backups"));
const Schedules = lazy(() => import("./pages/Schedules"));
const WorkflowDetail = lazy(() => import("./pages/WorkflowDetail"));
const Logs = lazy(() => import("./pages/Logs"));
const Activity = lazy(() => import("./pages/Activity"));
const Monitoring = lazy(() => import("./pages/Monitoring"));
const Uptime = lazy(() => import("./pages/Uptime"));
const Import = lazy(() => import("./pages/Import"));
const DiskCleanup = lazy(() => import("./pages/DiskCleanup"));
const StatusPage = lazy(() => import("./pages/StatusPage"));
const Users = lazy(() => import("./pages/Users"));
const ApiKeys = lazy(() => import("./pages/ApiKeys"));
const Audit = lazy(() => import("./pages/Audit"));
const Settings = lazy(() => import("./pages/Settings"));
const Changelog = lazy(() => import("./pages/Changelog"));
const AiAssistants = lazy(() => import("./pages/AiAssistants"));
const NetworkAccess = lazy(() => import("./pages/NetworkAccess"));
const NotFound = lazy(() => import("./pages/NotFound"));
const AutoUpdate = lazy(() => import("./pages/AutoUpdate"));
const Teams = lazy(() => import("./pages/Teams"));
const SsoProviders = lazy(() => import("./pages/SsoProviders"));
const SsoCallback = lazy(() => import("./pages/SsoCallback"));

function PageFallback() {
  return (
    <Box align="center" justify="center" height="100%" style={{ minHeight: 200 }}>
      <Spinner />
    </Box>
  );
}

export default function App() {
  // No Suspense around the whole tree: the shell (sidebar, top bar) must stay
  // mounted while a page chunk loads. Layout owns the boundary around <Outlet/>.
  return (
      <Routes>
        <Route path="/login" element={<Suspense fallback={<PageFallback />}><Login /></Suspense>} />
        <Route path="/login/sso" element={<Suspense fallback={<PageFallback />}><SsoCallback /></Suspense>} />
        <Route path="/setup" element={<Suspense fallback={<PageFallback />}><Setup /></Suspense>} />
        <Route path="/status" element={<Suspense fallback={<PageFallback />}><StatusPage /></Suspense>} />

        <Route
          element={
            <RequireAuth>
              <Layout />
            </RequireAuth>
          }
        >
          <Route path="/" element={<Dashboard />} />
          <Route path="/hosts" element={<Hosts />} />
          <Route path="/hosts/:id" element={<HostDetail />} />
          <Route path="/hosts/:hostId/cleanup" element={<DiskCleanup />} />
          <Route path="/containers/:id" element={<ContainerDetail />} />
          <Route path="/images" element={<Images />} />
          <Route path="/images/:id" element={<ImageDetail />} />
          <Route path="/volumes" element={<Volumes />} />
          {/* /volume-explorer must precede /volumes/:name so it is not captured. */}
          <Route path="/volume-explorer" element={<VolumeExplorer />} />
          <Route path="/volumes/:name" element={<VolumeDetail />} />
          <Route path="/networks" element={<Networks />} />
          {/* /networks/map must precede /networks/:id so it is not captured. */}
          <Route path="/networks/map" element={<FeatureGate feature="networkMap"><NetworkMap /></FeatureGate>} />
          <Route path="/exposure" element={<Exposure />} />
          <Route path="/networks/:id" element={<NetworkDetail />} />
          <Route path="/credentials" element={<Connections />} />
          <Route path="/registries" element={<Registries />} />
          <Route path="/registry-explorer" element={<RegistryExplorer />} />
          <Route path="/secrets" element={<Secrets />} />
          <Route path="/workloads" element={<Workloads />} />
          <Route path="/workloads/:appId" element={<WorkloadDetail />} />
          {/* /new must precede /:id so it is not captured by the :id param. */}
          <Route path="/deployments/new" element={<DeploymentCreate />} />
          <Route path="/templates" element={<FeatureGate feature="templates"><Templates /></FeatureGate>} />
          <Route path="/workflows" element={<Workflows />} />
          <Route path="/backups" element={<Backups />} />
          <Route path="/schedules" element={<Schedules />} />
          {/* /new must precede /:id so it is not captured by the :id param. */}
          <Route path="/workflows/new" element={<WorkflowDetail />} />
          <Route path="/workflows/:id" element={<WorkflowDetail />} />
          <Route path="/gitops" element={<FeatureGate feature="gitops"><GitOps /></FeatureGate>} />
          <Route path="/gitops/:id" element={<FeatureGate feature="gitops"><GitOps /></FeatureGate>} />
          <Route path="/logs" element={<Logs />} />
          <Route path="/activity" element={<Activity />} />
          <Route path="/monitoring" element={<Monitoring />} />
          <Route path="/uptime" element={<Uptime />} />
          <Route path="/import" element={<Import />} />
          <Route path="/channels" element={<Notifications />} />
          <Route path="/users" element={<Users />} />
          <Route path="/api-keys" element={<ApiKeys />} />
          <Route path="/audit" element={<Audit />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/changelog" element={<Changelog />} />
          <Route path="/ai" element={<AiAssistants />} />
          <Route path="/settings/network" element={<NetworkAccess />} />
          <Route path="/auto-update" element={<AutoUpdate />} />
          <Route path="/teams" element={<Teams />} />
          <Route path="/sso" element={<SsoProviders />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Routes>
  );
}
