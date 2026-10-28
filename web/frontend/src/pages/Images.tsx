import { useEffect, useState } from "react";
import {
  Badge,
  Box,
  Button,
  Callout,
  Checkbox,
  Code,
  Icon,
  InputToggle,
  Popup,
  Radio,
  RadioGroup,
  Select,
  Table,
  Tabs,
  Text,
  TextArea,
  TextInput,
  Tooltip,
  type Column,
} from "@/ui/kit";
import { Link, useNavigate } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "../auth/AuthContext";
import {
  buildImage,
  listHosts,
  listImages,
  listRegistries,
  prune,
  pullImage,
  removeImage,
  checkImageUpdate,
  listContainers,
  type ImageUpdateRow,
} from "../api/client";
import { describeUpdate, imageUpdate, normalizeRef, UPDATES_KEY, useContainerUpdates, type ImageUpdateState } from "../lib/updates";

type ImageFilter = "all" | "used" | "unused" | "untagged" | "update";
import ConfirmModal from "../components/ConfirmModal";
import HostSelectField, { defaultTargetHost } from "../components/HostSelectField";
import KeyValueEditor from "../components/KeyValueEditor";
import { RegistryImagePicker } from "../components/images/RegistryImagePicker";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import EmptyState from "../components/EmptyState";
import { ScanChips } from "../components/security/SeverityChips";
import { useHostScans } from "../components/security/useHostScans";
import { useHost, isAllHosts } from "../HostContext";
import { useHostScopedList, type WithHost } from "../useHostScopedList";
import { LIVE_REFETCH_MS } from "../lib/live";
import { realTags } from "../lib/images";
import type { BuildOptions, BuildResult, Container, Image } from "../types";
import { errorMessage, formatBytes, formatDate, shortId } from "../utils";
import { notify } from "../ui/toast";

// Row type for the list: an image optionally tagged with the host it came from
// (populated only in the aggregated "All hosts" view).
type ImageRow = Image & WithHost;

type BuildSource = "dockerfile" | "git";

interface BuildArgRow {
  key: string;
  value: string;
}

const DEFAULT_DOCKERFILE = "FROM alpine:latest\n\n";

export default function Images() {
  const { hostId } = useHost();
  const { user } = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const aggregated = isAllHosts(hostId);

  const isAdmin = user?.role === "admin";

  const [pullOpen, setPullOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<ImageRow | null>(null);
  const [forceRemove, setForceRemove] = useState(false);
  const [pruneOpen, setPruneOpen] = useState(false);
  const [pruneAll, setPruneAll] = useState(false);

  // Target host for the create/pull/build/prune modals. Since these actions are
  // always shown (even in the aggregated "All hosts" view), the user picks the
  // concrete host inside each modal instead of relying on the global selection.
  const [targetHost, setTargetHost] = useState("");

  // Build-image modal state.
  const isOperator = user?.role === "admin" || user?.role === "operator";
  const [buildOpen, setBuildOpen] = useState(false);
  const [buildTag, setBuildTag] = useState("");
  const [buildSource, setBuildSource] = useState<BuildSource>("dockerfile");
  const [dockerfile, setDockerfile] = useState(DEFAULT_DOCKERFILE);
  const [gitUrl, setGitUrl] = useState("");
  const [gitRef, setGitRef] = useState("");
  const [contextSubdir, setContextSubdir] = useState("");
  const [buildArgs, setBuildArgs] = useState<BuildArgRow[]>([]);
  const [noCache, setNoCache] = useState(false);
  const [push, setPush] = useState(false);
  const [pushRegistryId, setPushRegistryId] = useState("");
  const [buildResult, setBuildResult] = useState<BuildResult | null>(null);

  // Pull-image form: plain controlled state + inline validation.
  const [pullRef, setPullRef] = useState("");
  const [pullRegistryId, setPullRegistryId] = useState("");
  const [pullRefError, setPullRefError] = useState<string | null>(null);

  const imagesQuery = useHostScopedList<Image>(
    "images",
    hostId,
    (h) => listImages(h),
    { refetchInterval: LIVE_REFETCH_MS },
  );

  // Hosts list, used to compute the default target host for the modals. Shares
  // the ["hosts"] key with the Layout host-switcher and HostSelectField.
  const hostsQuery = useQuery({ queryKey: ["hosts"], queryFn: listHosts });
  const hosts = hostsQuery.data ?? [];

  // Initialize the target host each time a host-targeting modal opens: prefer the
  // global selection when it is a single host, else the first online host.
  useEffect(() => {
    if (pullOpen) setTargetHost(defaultTargetHost(hostId, hosts));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pullOpen]);
  useEffect(() => {
    if (buildOpen) setTargetHost(defaultTargetHost(hostId, hosts));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [buildOpen]);
  useEffect(() => {
    if (pruneOpen) setTargetHost(defaultTargetHost(hostId, hosts));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pruneOpen]);

  // The effective host for a row's actions/links: its own host in the aggregated
  // view, otherwise the single selected host.
  const rowHost = (image: ImageRow) => image._hostId ?? hostId;
  const detailPath = (image: ImageRow) =>
    `/images/${encodeURIComponent(image.id)}?host=${encodeURIComponent(rowHost(image))}`;

  const invalidate = () =>
    queryClient.invalidateQueries({
      predicate: (q) => String(q.queryKey[0]).startsWith("images"),
    });

  // Registries are admin-only; the Select is hidden for non-admins.
  const registriesQuery = useQuery({
    queryKey: ["registries"],
    queryFn: listRegistries,
    enabled: isAdmin,
  });

  const resetPullForm = () => {
    setPullRef("");
    setPullRegistryId("");
    setPullRefError(null);
  };

  const pullMutation = useMutation({
    mutationFn: (values: { ref: string; registryId: string }) =>
      pullImage(targetHost, values.ref, values.registryId || undefined),
    onSuccess: (_data, values) => {
      notify({
        color: "green",
        title: "Image pulled",
        message: `Pulled ${values.ref}`,
      });
      resetPullForm();
      setPullOpen(false);
      invalidate();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Pull failed",
        message: errorMessage(err, "Could not pull image"),
      });
    },
  });

  const removeMutation = useMutation({
    mutationFn: ({ host, id, force }: { host: string; id: string; force: boolean }) =>
      removeImage(host, id, force),
    onSuccess: () => {
      notify({
        color: "green",
        title: "Image removed",
        message: "The image was removed",
      });
      setDeleteTarget(null);
      setForceRemove(false);
      invalidate();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Remove failed",
        message: errorMessage(err, "Could not remove image"),
      });
    },
  });

  const pruneMutation = useMutation({
    mutationFn: (all: boolean) => prune(targetHost, "images", all),
    onSuccess: (res) => {
      notify({
        color: "green",
        title: "Images pruned",
        message: `Reclaimed ${formatBytes(res.spaceReclaimed)}`,
      });
      setPruneOpen(false);
      invalidate();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Prune failed",
        message: errorMessage(err, "Could not prune images"),
      });
    },
  });

  const buildMutation = useMutation({
    mutationFn: (opts: BuildOptions) => buildImage(targetHost, opts),
    onSuccess: (result) => {
      setBuildResult(result);
      if (result.success) {
        notify({
          color: "green",
          title: "Image built",
          message: push
            ? `Built and pushed ${buildTag.trim()}`
            : `Built ${buildTag.trim()}`,
        });
        invalidate();
      } else {
        notify({
          color: "red",
          title: "Build failed",
          message: "The build did not complete. See the output for details.",
        });
      }
    },
    onError: (err) => {
      setBuildResult(null);
      notify({
        color: "red",
        title: "Build failed",
        message: errorMessage(err, "Could not start the build"),
      });
    },
  });

  const handlePull = (e: React.FormEvent) => {
    e.preventDefault();
    const ref = pullRef.trim();
    if (ref.length === 0) {
      setPullRefError("Image reference is required");
      return;
    }
    setPullRefError(null);
    pullMutation.mutate({
      ref,
      registryId: pullRegistryId,
    });
  };

  const resetBuildForm = () => {
    setBuildTag("");
    setBuildSource("dockerfile");
    setDockerfile(DEFAULT_DOCKERFILE);
    setGitUrl("");
    setGitRef("");
    setContextSubdir("");
    setBuildArgs([]);
    setNoCache(false);
    setPush(false);
    setPushRegistryId("");
    setBuildResult(null);
    buildMutation.reset();
  };

  const openBuild = () => {
    resetBuildForm();
    setBuildOpen(true);
  };

  const closeBuild = () => {
    if (buildMutation.isPending) return;
    setBuildOpen(false);
  };

  const handleBuild = (e: React.FormEvent) => {
    e.preventDefault();
    const tag = buildTag.trim();
    if (tag.length === 0) {
      notify({
        color: "red",
        title: "Tag is required",
        message: "Enter a tag for the built image (e.g. myapp:latest).",
      });
      return;
    }
    if (buildSource === "git" && gitUrl.trim().length === 0) {
      notify({
        color: "red",
        title: "Git URL is required",
        message: "Enter the repository URL to clone and build.",
      });
      return;
    }
    if (buildSource === "dockerfile" && dockerfile.trim().length === 0) {
      notify({
        color: "red",
        title: "Dockerfile is required",
        message: "Enter the Dockerfile contents to build.",
      });
      return;
    }
    if (push && !pushRegistryId) {
      notify({
        color: "red",
        title: "Registry is required",
        message: "Select the registry to push the built image to.",
      });
      return;
    }

    const args: Record<string, string> = {};
    for (const row of buildArgs) {
      const key = row.key.trim();
      if (key.length > 0) args[key] = row.value;
    }

    const opts: BuildOptions = {
      tag,
      noCache,
      push,
      ...(Object.keys(args).length > 0 ? { buildArgs: args } : {}),
      ...(push && pushRegistryId ? { registryId: pushRegistryId } : {}),
    };
    if (buildSource === "git") {
      opts.gitUrl = gitUrl.trim();
      if (gitRef.trim()) opts.gitRef = gitRef.trim();
      if (contextSubdir.trim()) opts.contextSubdir = contextSubdir.trim();
    } else {
      opts.dockerfile = dockerfile;
    }

    buildMutation.mutate(opts);
  };

  const addBuildArg = () =>
    setBuildArgs((rows) => [...rows, { key: "", value: "" }]);
  const updateBuildArg = (index: number, patch: Partial<BuildArgRow>) =>
    setBuildArgs((rows) =>
      rows.map((r, i) => (i === index ? { ...r, ...patch } : r)),
    );
  const removeBuildArg = (index: number) =>
    setBuildArgs((rows) => rows.filter((_, i) => i !== index));

  const registryOptions = (registriesQuery.data ?? []).map((registry) => ({
    value: registry.id,
    label: `${registry.name} (${registry.url})`,
  }));

  const closePull = () => {
    if (pullMutation.isPending) return;
    resetPullForm();
    setPullOpen(false);
  };

  const scans = useHostScans(hostId);

  // Which images have a newer digest in their registry, from the update report
  // of the containers that run them. Check updates asks the registries again,
  // bypassing the server's digest cache, and fills the column when it is done.
  const updates = useContainerUpdates(hostId);
  // A check of one image (the row's icon) is fresher than the container
  // report and covers images no container runs: it wins while it is there.
  const [imageChecks, setImageChecks] = useState<Record<string, ImageUpdateRow[]>>({});
  const imageKey = (image: ImageRow) => `${rowHost(image)}/${image.id}`;
  const updateOf = (image: ImageRow): { state: ImageUpdateState; tip: string } => {
    const own = imageChecks[imageKey(image)];
    if (own) {
      const state: ImageUpdateState = !own.length ? "none" : own.some((r) => r.updateAvailable) ? "update" : own.some((r) => r.error) ? "error" : "current";
      const short = (d: string) => (d ? d.replace(/^sha256:/, "").slice(0, 12) : "—");
      const tip = own.map((r) => `${r.tag}: ${r.error || (r.updateAvailable ? `registry has ${short(r.remoteDigest)}, here ${short(r.localDigest)}` : `up to date (${short(r.localDigest)})`)}`).join("\n");
      return { state, tip };
    }
    const u = imageUpdate(updates.forHost(rowHost(image)), realTags(image));
    return { state: u.state, tip: u.rows.map((r) => `${r.containerName}: ${describeUpdate(r)}`).join("\n") };
  };
  const checkOne = useMutation({
    mutationFn: (image: ImageRow) => checkImageUpdate(rowHost(image), image.id),
    onSuccess: (rows, image) => {
      setImageChecks((m) => ({ ...m, [imageKey(image)]: rows }));
      const name = realTags(image)[0] ?? shortId(image.id);
      const newer = rows.filter((r) => r.updateAvailable);
      const failed = rows.find((r) => r.error);
      notify(
        newer.length ? { color: "blue", title: `${name}: update available`, message: "The registry has a newer image. Pull it from the row." }
          : failed ? { color: "yellow", title: `${name}: couldn't check`, message: failed.error ?? "" }
            : rows.length ? { color: "green", title: `${name} is up to date`, message: "Same digest as the registry." }
              : { color: "gray", title: `${name} has no tag`, message: "There is nothing to compare with a registry." },
      );
    },
    onError: (err) => notify({ color: "red", title: "Check failed", message: errorMessage(err, "Could not check the image") }),
  });
  // Pull the tags that have a newer image. Containers keep the image they
  // started with until they are updated (the workload's Update does that).
  const pullNewer = useMutation({
    mutationFn: async (image: ImageRow) => {
      const tags = (imageChecks[imageKey(image)] ?? []).filter((r) => r.updateAvailable).map((r) => r.tag);
      const list = tags.length ? tags : realTags(image);
      for (const t of list) await pullImage(rowHost(image), t);
      return list;
    },
    onSuccess: (tags, image) => {
      notify({ color: "green", title: `Pulled ${tags.join(", ")}`, message: "Containers that use it switch when they are updated or redeployed." });
      setImageChecks((m) => {
        const next = { ...m };
        delete next[imageKey(image)];
        return next;
      });
      void queryClient.invalidateQueries({ queryKey: ["images"] });
      void queryClient.invalidateQueries({ queryKey: [UPDATES_KEY] });
    },
    onError: (err) => notify({ color: "red", title: "Pull failed", message: errorMessage(err, "Could not pull the image") }),
  });
  const UPDATE_RANK: Record<ImageUpdateState, number> = { update: 3, error: 2, current: 1, none: 0 };

  const allImages = imagesQuery.data ?? [];

  // Which images a container (running or stopped) uses, as Docker counts it:
  // a container names its image by tag, or by id once the tag moved on.
  const containersQuery = useHostScopedList<Container>("containers:all", hostId, (h) => listContainers(h, true), { refetchInterval: LIVE_REFETCH_MS });
  const usedBy = (image: ImageRow): number => {
    const host = rowHost(image);
    const tags = new Set(realTags(image).map(normalizeRef));
    const bare = image.id.replace(/^sha256:/, "");
    return (containersQuery.data ?? []).filter((c) => {
      if ((c._hostId ?? hostId) !== host) return false;
      const ref = c.image || "";
      const id = ref.replace(/^sha256:/, "");
      return tags.has(normalizeRef(ref)) || (id.length >= 12 && /^[0-9a-f]+$/.test(id) && bare.startsWith(id));
    }).length;
  };
  const FILTERS: { id: ImageFilter; label: string; match: (i: ImageRow) => boolean }[] = [
    { id: "all", label: "All", match: () => true },
    { id: "used", label: "In use", match: (i) => usedBy(i) > 0 },
    { id: "unused", label: "Unused", match: (i) => usedBy(i) === 0 },
    { id: "untagged", label: "Untagged", match: (i) => realTags(i).length === 0 },
    { id: "update", label: "Update available", match: (i) => updateOf(i).state === "update" },
  ];
  const [filter, setFilter] = useState<ImageFilter>("all");
  const images = allImages.filter(FILTERS.find((f) => f.id === filter)!.match);

  // Several images picked at once, for one remove. Keys are host/id, so the
  // aggregated view can mix hosts; a filter change keeps only what still shows.
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const shown = new Set(images.map(imageKey));
  const picked = images.filter((i) => selected.has(imageKey(i)));
  const allPicked = images.length > 0 && picked.length === images.length;
  const pick = (image: ImageRow, on: boolean) =>
    setSelected((cur) => {
      const next = new Set([...cur].filter((k) => shown.has(k)));
      if (on) next.add(imageKey(image));
      else next.delete(imageKey(image));
      return next;
    });
  const [bulkOpen, setBulkOpen] = useState(false);
  const [bulkForce, setBulkForce] = useState(false);
  const bulkRemove = useMutation({
    mutationFn: async ({ rows, force }: { rows: ImageRow[]; force: boolean }) => {
      const failed: string[] = [];
      for (const image of rows) {
        try {
          await removeImage(rowHost(image), image.id, force);
        } catch (err) {
          failed.push(`${realTags(image)[0] ?? shortId(image.id)}: ${errorMessage(err, "could not remove")}`);
        }
      }
      return { removed: rows.length - failed.length, failed };
    },
    onSuccess: ({ removed, failed }) => {
      notify(
        failed.length
          ? { color: "yellow", title: `Removed ${removed}, ${failed.length} failed`, message: failed.slice(0, 3).join("\n") + (failed.length > 3 ? `\n…and ${failed.length - 3} more` : "") }
          : { color: "green", title: `Removed ${removed} image${removed === 1 ? "" : "s"}`, message: "Their disk space is free." },
      );
      setBulkOpen(false);
      setBulkForce(false);
      setSelected(new Set());
      invalidate();
    },
  });

  const hostColumn: Column<ImageRow> = {
    key: "host",
    header: "Host",
    sortable: true,
    accessor: (image) => image._hostName ?? "",
    cell: (image) => (
      <Badge variant="light" color="magenta" iconLeft="dns-outline">
        {image._hostName ?? "—"}
      </Badge>
    ),
  };

  const columns: Column<ImageRow>[] = [
    {
      key: "pick",
      header: (
        <Checkbox
          aria-label={allPicked ? "Unselect every image shown" : "Select every image shown"}
          checked={allPicked}
          onChange={(e) => setSelected(e.target.checked ? new Set(images.map(imageKey)) : new Set())}
        />
      ),
      width: 36,
      cell: (image) => (
        <span onClick={(e) => e.stopPropagation()}>
          <Checkbox
            aria-label={`Select ${realTags(image)[0] ?? shortId(image.id)}`}
            checked={selected.has(imageKey(image))}
            onChange={(e) => pick(image, e.target.checked)}
          />
        </span>
      ),
    },
    {
      key: "repoTags",
      header: "Repository tags",
      sortable: true,
      // Join every tag so global search matches any of them (not just the first).
      accessor: (image) => realTags(image).join(" "),
      grow: true,
      cell: (image) => {
        const tags = realTags(image);
        if (tags.length === 0) {
          return (
            <Link
              to={detailPath(image)}
              className="row-link"
              onClick={(e) => e.stopPropagation()}
            >
              <Badge color="gray" variant="light">
                &lt;none&gt;
              </Badge>
            </Link>
          );
        }
        // Render every tag as its own info-coloured link (the "link" column
        // type), consistent with the Name links on Networks/Volumes.
        return (
          <Box direction="row" wrap gap={2} align="center">
            {tags.map((tag) => (
              <Link
                key={tag}
                to={detailPath(image)}
                className="cell-link"
                onClick={(e) => e.stopPropagation()}
              >
                {tag}
              </Link>
            ))}
          </Box>
        );
      },
    },
    ...(aggregated ? [hostColumn] : []),
    {
      key: "id",
      header: "ID",
      accessor: (image) => image.id,
      cell: (image) => <Code>{shortId(image.id)}</Code>,
    },
    {
      key: "vulns",
      header: "Vulnerabilities",
      accessor: (image) => {
        const s = scans.byImage(image._hostId ?? hostId, image.id)?.scan;
        return s?.status === "ok" ? s.counts.critical * 1e6 + s.counts.high * 1e3 + s.counts.medium : -1;
      },
      sortable: true,
      cell: (image) => {
        const s = scans.byImage(image._hostId ?? hostId, image.id);
        return <ScanChips scan={s?.scan} running={s?.running} emptyLabel="—" />;
      },
    },
    {
      key: "update",
      header: "Update",
      sortable: true,
      accessor: (image) => UPDATE_RANK[updateOf(image).state],
      cell: (image) => {
        const u = updateOf(image);
        if (u.state === "none") return <span title="No container runs it, or it was not checked yet">—</span>;
        const tip = u.tip;
        const [color, label] = u.state === "update" ? ["blue", "Update available"] : u.state === "error" ? ["yellow", "Couldn't check"] : ["gray", "Up to date"];
        return (
          <Tooltip content={tip}>
            <Badge variant="light" color={color as "blue"}>{label}</Badge>
          </Tooltip>
        );
      },
    },
    {
      key: "size",
      header: "Size",
      sortable: true,
      accessor: (image) => image.size,
      cell: (image) => formatBytes(image.size),
    },
    {
      key: "created",
      header: "Created",
      sortable: true,
      accessor: (image) => image.created,
      cell: (image) => formatDate(image.created),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (image) => (
        <Box
          direction="row"
          onClick={(e) => e.stopPropagation()}
          style={{ display: "inline-flex" }}
        >
          {renderRowActions(image)}
        </Box>
      ),
    },
  ];

  const renderRowActions = (image: ImageRow) => (
    <>
      {realTags(image).length > 0 && (
        <Tooltip content="Check this image for an update now">
          <Button
            variant="clear"
            iconLeft="upgrade-rounded"
            loading={checkOne.isPending && checkOne.variables?.id === image.id}
            onClick={() => checkOne.mutate(image)}
            aria-label={`Check ${realTags(image)[0]} for an update`}
          />
        </Tooltip>
      )}
      {isOperator && updateOf(image).state === "update" && (
        <Tooltip content="Pull the newer image">
          <Button
            variant="clear"
            color="info"
            iconLeft="download-rounded"
            loading={pullNewer.isPending && pullNewer.variables?.id === image.id}
            onClick={() => pullNewer.mutate(image)}
            aria-label={`Pull the newer ${realTags(image)[0]}`}
          />
        </Tooltip>
      )}
      <Tooltip content="Remove image">
        <Button
          color="danger"
          variant="clear"
          iconLeft="delete-outline-rounded"
          onClick={() => {
            setForceRemove(false);
            setDeleteTarget(image);
          }}
          aria-label="Remove image"
        />
      </Tooltip>
    </>
  );

  return (
    <Box direction="column" gap={6}>
      <PageHeader
        title="Images"
        description="Pull, inspect and prune container images on this host."
        icon={<Icon name="image-outline-rounded" size="xl" />}
        actions={
          <>
            <Tooltip content="Refresh">
              <Button
                variant="outline"
                iconLeft="refresh-rounded"
                onClick={() => imagesQuery.refetch()}
                loading={imagesQuery.isFetching}
                aria-label="Refresh images"
              />
            </Tooltip>
            <Button
              variant="outline"
              color="danger"
              iconLeft="delete-outline-rounded"
              onClick={() => {
                setPruneAll(false);
                setPruneOpen(true);
              }}
            >
              Prune unused
            </Button>
            {isOperator && (
              <Button
                variant="outline"
                iconLeft="upgrade-rounded"
                onClick={() => void updates.checkNow()}
                loading={updates.checking}
              >
                Check updates
              </Button>
            )}
            {isOperator && (
              <Button
                variant="outline"
                iconLeft="build-outline-rounded"
                onClick={openBuild}
              >
                Build image
              </Button>
            )}
            <Button
              color="info"
              iconLeft="download-rounded"
              onClick={() => setPullOpen(true)}
            >
              Pull image
            </Button>
          </>
        }
      />

      {imagesQuery.isError && (
        <Callout
          color="danger"
          iconLeft={<Icon name="error-outline-rounded" />}
          title="Failed to load images"
        >
          {errorMessage(imagesQuery.error, "Could not load images for this host.")}
        </Callout>
      )}

      <SectionCard
        plain
        title="Images"
        icon={<Icon name="image-outline-rounded" />}
        actions={
          <Badge variant="light" color="gray" size="lg">
            {images.length}
          </Badge>
        }
        noBodyPadding
      >
        {imagesQuery.isError ? null : (
          <Table<ImageRow>
            data={images}
            columns={columns}
            rowKey={(image) => `${image._hostId ?? ""}/${image.id}`}
            toolbar={
              <Box direction="row" gap={2} align="center" wrap>
                {FILTERS.map((f) => (
                  <Button
                    key={f.id}
                    size="sm"
                    variant={filter === f.id ? "solid" : "outline"}
                    aria-pressed={filter === f.id}
                    onClick={() => {
                      setFilter(f.id);
                      setSelected(new Set());
                    }}
                  >
                    {f.label} · {allImages.filter(f.match).length}
                  </Button>
                ))}
                {picked.length > 0 && (
                  <>
                    <Text size="sm" color="muted">{picked.length} selected</Text>
                    <Button size="sm" variant="clear" onClick={() => setSelected(new Set())}>Clear</Button>
                    <Button size="sm" color="danger" iconLeft="delete-outline-rounded" onClick={() => setBulkOpen(true)}>
                      Remove {picked.length}
                    </Button>
                  </>
                )}
              </Box>
            }
            onRowClick={(image) => navigate(detailPath(image))}
            globalSearch
            pagination
            defaultPageSize={50}
            pageSizeOptions={[10, 25, 50, 100]}
            loading={imagesQuery.isLoading}
            stickyHeader
            striped
            hoverable
            emptyState={
              <EmptyState
                icon={<Icon name="image-outline-rounded" size="xl" />}
                title="No images found"
                description="No images are present on this host yet. Pull one to get started."
                action={
                  <Button
                    color="info"
                    iconLeft="download-rounded"
                    onClick={() => setPullOpen(true)}
                  >
                    Pull image
                  </Button>
                }
              />
            }
          />
        )}
      </SectionCard>

      <Popup
        open={buildOpen}
        onOpenChange={(o) => {
          if (!o) closeBuild();
        }}
        size="xl"
        closeOnBackdropClick={!buildMutation.isPending}
        closeOnEsc={!buildMutation.isPending}
      >
        <Popup.Content>
          <Popup.Header>
            <Popup.Title>Build image</Popup.Title>
            {!buildMutation.isPending && <Popup.Close />}
          </Popup.Header>
          <form onSubmit={handleBuild}>
            <Popup.Body>
              <Box direction="column" gap={4}>
                <HostSelectField
                  value={targetHost}
                  onChange={setTargetHost}
                  description="Host that will build the image."
                  disabled={buildMutation.isPending}
                />

                <TextInput
                  label="Tag"
                  placeholder="myapp:latest"
                  description="Name and tag for the built image, e.g. ghcr.io/owner/app:1.0"
                  required
                  data-autofocus
                  disabled={buildMutation.isPending}
                  value={buildTag}
                  onChange={(e) => setBuildTag(e.target.value)}
                />

                <Box>
                  <Text size="sm" weight="medium" style={{ marginBottom: 6 }}>
                    Source
                  </Text>
                  <Tabs
                    variant="segmented"
                    fullWidth
                    value={buildSource}
                    onValueChange={(v) => setBuildSource(v as BuildSource)}
                  >
                    <Tabs.List>
                      <Tabs.Trigger
                        value="dockerfile"
                        disabled={buildMutation.isPending}
                      >
                        Dockerfile
                      </Tabs.Trigger>
                      <Tabs.Trigger
                        value="git"
                        disabled={buildMutation.isPending}
                      >
                        Git repository
                      </Tabs.Trigger>
                    </Tabs.List>
                  </Tabs>
                </Box>

                {buildSource === "dockerfile" ? (
                  <TextArea
                    label="Dockerfile"
                    description="Inline Dockerfile contents to build."
                    placeholder="FROM alpine:latest"
                    rows={8}
                    disabled={buildMutation.isPending}
                    value={dockerfile}
                    onChange={(e) => setDockerfile(e.target.value)}
                    style={{ fontFamily: "monospace", fontSize: 13 }}
                  />
                ) : (
                  <Box direction="column" gap={4}>
                    <TextInput
                      label="Git URL"
                      placeholder="https://github.com/owner/repo.git"
                      description="Repository cloned with --depth 1 before building."
                      required
                      disabled={buildMutation.isPending}
                      value={gitUrl}
                      onChange={(e) => setGitUrl(e.target.value)}
                    />
                    <Box direction="row" gap={4} align="start">
                      <TextInput
                        label="Git ref"
                        placeholder="main"
                        description="Branch or tag (optional)."
                        disabled={buildMutation.isPending}
                        value={gitRef}
                        onChange={(e) => setGitRef(e.target.value)}
                        style={{ flex: 1 }}
                      />
                      <TextInput
                        label="Context subdirectory"
                        placeholder="services/api"
                        description="Build context within the repo (optional)."
                        disabled={buildMutation.isPending}
                        value={contextSubdir}
                        onChange={(e) => setContextSubdir(e.target.value)}
                        style={{ flex: 1 }}
                      />
                    </Box>
                  </Box>
                )}

                <KeyValueEditor
                  label="Build args"
                  emptyText="No build arguments."
                  removeAriaLabel="Remove build arg"
                  keyPlaceholder="KEY"
                  valuePlaceholder="value"
                  disabled={buildMutation.isPending}
                  rows={buildArgs}
                  onAdd={addBuildArg}
                  onUpdate={updateBuildArg}
                  onRemove={removeBuildArg}
                />

                <InputToggle
                  label="No cache"
                  description="Build without using the Docker layer cache."
                  checked={noCache}
                  disabled={buildMutation.isPending}
                  onChange={(e) => setNoCache(e.target.checked)}
                />

                <InputToggle
                  label="Push to registry"
                  description="Push the built image after a successful build."
                  checked={push}
                  disabled={buildMutation.isPending}
                  onChange={(e) => {
                    const on = e.target.checked;
                    setPush(on);
                    if (!on) setPushRegistryId("");
                  }}
                />

                {push &&
                  (isAdmin ? (
                    <Select
                      label="Registry"
                      description="Stored credentials used to authenticate the push."
                      placeholder="Select a registry"
                      options={registryOptions}
                      disabled={buildMutation.isPending}
                      value={pushRegistryId || undefined}
                      onChange={(v) => setPushRegistryId(v)}
                    />
                  ) : (
                    <Callout
                      color="warning"
                      iconLeft={<Icon name="error-outline-rounded" />}
                    >
                      Selecting a registry for push requires administrator
                      privileges.
                    </Callout>
                  ))}

                {buildResult && (
                  <Box direction="column" gap={1}>
                    <Box direction="row" wrap gap={1} align="center">
                      <Text size="sm" weight="medium">
                        Output
                      </Text>
                      {buildResult.success ? (
                        <Badge
                          color="green"
                          variant="light"
                          iconLeft="check-circle-outline-rounded"
                        >
                          Success
                        </Badge>
                      ) : (
                        <Badge
                          color="red"
                          variant="light"
                          iconLeft="cancel-outline-rounded"
                        >
                          Failed
                        </Badge>
                      )}
                      {buildResult.success && buildResult.imageId && (
                        <Code>{shortId(buildResult.imageId)}</Code>
                      )}
                    </Box>
                    <Box
                      display="block"
                      style={{ maxHeight: 320, overflow: "auto" }}
                    >
                      <Code
                        block
                        style={{
                          whiteSpace: "pre-wrap",
                          wordBreak: "break-word",
                        }}
                      >
                        {buildResult.output || "(no output)"}
                      </Code>
                    </Box>
                  </Box>
                )}
              </Box>
            </Popup.Body>
            <Popup.Footer>
              <Box direction="row" wrap justify="end" gap={2}>
                <Button
                  variant="outline"
                  onClick={closeBuild}
                  disabled={buildMutation.isPending}
                >
                  {buildResult ? "Close" : "Cancel"}
                </Button>
                <Button
                  color="info"
                  type="submit"
                  loading={buildMutation.isPending}
                  disabled={targetHost === ""}
                >
                  Build
                </Button>
              </Box>
            </Popup.Footer>
          </form>
        </Popup.Content>
      </Popup>

      <Popup
        open={pullOpen}
        onOpenChange={(o) => {
          if (!o) closePull();
        }}
        closeOnBackdropClick={!pullMutation.isPending}
        closeOnEsc={!pullMutation.isPending}
      >
        <Popup.Content>
          <Popup.Header>
            <Popup.Title>Pull image</Popup.Title>
            {!pullMutation.isPending && <Popup.Close />}
          </Popup.Header>
          <form onSubmit={handlePull}>
            <Popup.Body>
              <Box direction="column" gap={4}>
                <HostSelectField
                  value={targetHost}
                  onChange={setTargetHost}
                  description="Host to pull the image onto."
                  disabled={pullMutation.isPending}
                />

                <RegistryImagePicker
                  registries={isAdmin ? registriesQuery.data ?? [] : undefined}
                  registryId={pullRegistryId}
                  onRegistryChange={setPullRegistryId}
                  value={pullRef}
                  onChange={(v) => {
                    setPullRef(v);
                    if (pullRefError) setPullRefError(null);
                  }}
                  error={pullRefError}
                  disabled={pullMutation.isPending}
                />
              </Box>
            </Popup.Body>
            <Popup.Footer>
              <Box direction="row" wrap justify="end" gap={2}>
                <Button
                  variant="outline"
                  onClick={closePull}
                  disabled={pullMutation.isPending}
                >
                  Cancel
                </Button>
                <Button
                  color="info"
                  type="submit"
                  loading={pullMutation.isPending}
                  disabled={targetHost === ""}
                >
                  Pull
                </Button>
              </Box>
            </Popup.Footer>
          </form>
        </Popup.Content>
      </Popup>

      <ConfirmModal
        opened={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title="Remove image"
        confirmLabel="Remove"
        loading={removeMutation.isPending}
        lockWhileLoading
        onConfirm={() => {
          if (deleteTarget)
            removeMutation.mutate({
              host: rowHost(deleteTarget),
              id: deleteTarget.id,
              force: forceRemove,
            });
        }}
        message={
          <>
            Are you sure you want to remove{" "}
            <Code>{realTags(deleteTarget)[0] ?? shortId(deleteTarget?.id)}</Code>? This
            action cannot be undone.
          </>
        }
      >
        <Checkbox
          label="Force remove (even if used by stopped containers)"
          checked={forceRemove}
          onChange={(e) => setForceRemove(e.target.checked)}
          disabled={removeMutation.isPending}
        />
      </ConfirmModal>

      <ConfirmModal
        opened={bulkOpen}
        onClose={() => setBulkOpen(false)}
        title={`Remove ${picked.length} image${picked.length === 1 ? "" : "s"}`}
        confirmLabel="Remove"
        loading={bulkRemove.isPending}
        lockWhileLoading
        onConfirm={() => bulkRemove.mutate({ rows: picked, force: bulkForce })}
        message={
          <>
            {picked.slice(0, 8).map((i) => (
              <div key={imageKey(i)}><Code>{realTags(i)[0] ?? shortId(i.id)}</Code></div>
            ))}
            {picked.length > 8 && <div>…and {picked.length - 8} more.</div>}
            {picked.some((i) => usedBy(i) > 0) && (
              <div style={{ marginTop: 8 }}>
                {picked.filter((i) => usedBy(i) > 0).length} of them {picked.filter((i) => usedBy(i) > 0).length === 1 ? "is" : "are"} used by a container: Docker refuses those unless forced, and a running container's image is never removed.
              </div>
            )}
            <div style={{ marginTop: 8 }}>This cannot be undone. An image that fails is reported, the rest still go.</div>
          </>
        }
      >
        <Checkbox
          label="Force remove (even if used by stopped containers)"
          checked={bulkForce}
          onChange={(e) => setBulkForce(e.target.checked)}
          disabled={bulkRemove.isPending}
        />
      </ConfirmModal>
      <ConfirmModal
        opened={pruneOpen}
        onClose={() => setPruneOpen(false)}
        title="Prune images"
        confirmLabel="Prune"
        loading={pruneMutation.isPending}
        lockWhileLoading
        onConfirm={() => {
          // Guard: never fire without a target host (no host available).
          if (targetHost === "") return;
          pruneMutation.mutate(pruneAll);
        }}
        message="Reclaim disk space by removing unused images on the selected host."
      >
        <HostSelectField
          value={targetHost}
          onChange={setTargetHost}
          description="Host to prune images on."
          disabled={pruneMutation.isPending}
        />
        <RadioGroup
          value={pruneAll ? "all" : "dangling"}
          onChange={(v) => setPruneAll(v === "all")}
          orientation="vertical"
        >
          <Radio value="dangling" label="Dangling only (untagged layers)" />
          <Radio
            value="all"
            label="All unused (any image not used by a container)"
          />
        </RadioGroup>
      </ConfirmModal>
    </Box>
  );
}
