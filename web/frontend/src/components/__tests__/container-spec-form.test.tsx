import { screen, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import {
  ContainerSpecForm,
  containerSpecFormFromSpec,
  containerSpecFormToSpec,
  emptyContainerSpecForm,
  validateContainerSpecForm,
  type ContainerSpecFormValue,
} from "../ContainerSpecForm";
import { joinWords, splitWords } from "../../lib/shellwords";
import { renderWithProviders } from "../../test/render";
import { choose } from "../../test/ui";

let latest: ContainerSpecFormValue = emptyContainerSpecForm();
function H({ initial }: { initial: ContainerSpecFormValue }) {
  const [v, setV] = useState(initial);
  latest = v;
  return (
    <ContainerSpecForm
      value={v}
      onChange={(x) => {
        latest = x;
        setV(x);
      }}
    />
  );
}

describe("shell words", () => {
  it("round-trips arguments with spaces, quotes and backslashes", () => {
    const argv = ["nginx", "-g", "daemon off;", 'say "hi"', "a\\b", "", "it's"];
    expect(splitWords(joinWords(argv))).toEqual(argv);
    expect(splitWords(`sh -c 'echo $HOME' x\\ y`)).toEqual(["sh", "-c", "echo $HOME", "x y"]);
    expect(splitWords("   ")).toEqual([]);
  });
});

describe("container spec form helpers", () => {
  it("converts a spec to the form and back without losing arguments", () => {
    const spec = {
      image: "nginx:1.27", env: [{ key: "A", value: "1" }], ports: [{ hostPort: 8080, containerPort: 80, protocol: "tcp" as const }],
      volumes: [{ source: "data", target: "/data", readOnly: true }], networks: ["web"], restartPolicy: "always" as const,
      command: ["nginx", "-g", "daemon off;"], entrypoint: ["/docker-entrypoint.sh"], workingDir: "/app", user: "1000",
      cpuLimit: 1.5, memoryLimitMB: 512, memoryReservationMB: 256, privileged: true, gpus: "all", tty: true, openStdin: true,
      pullImage: true, extraArgs: ["--init", "--cap-add NET_ADMIN"], labels: {},
    };
    const form = containerSpecFormFromSpec(spec, "web");
    expect(form.command).toBe(`nginx -g "daemon off;"`);
    expect(containerSpecFormToSpec(form)).toMatchObject({ ...spec, labels: {} });
    const empty = containerSpecFormToSpec(containerSpecFormFromSpec(null, "x"));
    expect(empty).toMatchObject({ image: "", command: [], entrypoint: undefined, cpuLimit: undefined, extraArgs: undefined });
  });

  it("validates image and ports", () => {
    const f = emptyContainerSpecForm();
    expect(validateContainerSpecForm(f)).toMatch(/image is required/i);
    f.image = "nginx";
    f.ports = [{ hostPort: 0, containerPort: 0, protocol: "tcp" }];
    expect(validateContainerSpecForm(f)).toMatch(/container port between 1 and 65535/i);
    f.ports = [{ hostPort: 70000, containerPort: 80, protocol: "tcp" }];
    expect(validateContainerSpecForm(f)).toMatch(/host port must be between/i);
    f.ports = [{ hostPort: 0, containerPort: 80, protocol: "tcp" }];
    expect(validateContainerSpecForm(f)).toBeNull();
  });
});

describe("container spec form", () => {
  it("edits every tab", async () => {
    const { user } = renderWithProviders(<H initial={{ ...emptyContainerSpecForm(), image: "nginx" }} />);
    await user.type(screen.getByLabelText(/^name/i), "web");
    await choose(user, /^registry/i, /private/i);
    expect(latest.registryId).toBe("r-priv");

    // Ports
    await user.click(screen.getByRole("button", { name: /map additional port/i }));
    await user.type(screen.getByPlaceholderText("container"), "80");
    await user.type(screen.getByPlaceholderText(/host \(0 = random\)/i), "8080");
    await choose(user, /protocol/i, /^udp$/i);
    expect(latest.ports).toEqual([{ hostPort: 8080, containerPort: 80, protocol: "udp" }]);
    await user.click(screen.getByRole("button", { name: "Remove port" }));
    expect(latest.ports).toEqual([]);

    // Command & runtime
    await user.type(screen.getByLabelText("Command"), "nginx -g 'daemon off;'");
    await user.type(screen.getByLabelText(/^entrypoint/i), "/bin/sh -c");
    await user.type(screen.getByLabelText(/working directory/i), "/srv");
    await user.type(screen.getByLabelText(/^user/i), "1000");
    for (const name of [/privileged/i, /^tty/i, /open stdin/i, /pull image before create/i]) {
      await user.click(screen.getByRole("switch", { name }));
    }
    await choose(user, /restart policy/i, /always/i);
    expect(containerSpecFormToSpec(latest)).toMatchObject({
      command: ["nginx", "-g", "daemon off;"], entrypoint: ["/bin/sh", "-c"], workingDir: "/srv", user: "1000",
      privileged: true, tty: true, openStdin: true, pullImage: true, restartPolicy: "always",
    });

    // Volumes
    await user.click(screen.getByRole("tab", { name: /volumes/i }));
    await user.click(screen.getByRole("button", { name: /add volume/i }));
    await user.type(screen.getByPlaceholderText(/source \/ named volume/i), "data");
    await user.type(screen.getByPlaceholderText("/container/path"), "/data");
    await user.click(screen.getByRole("checkbox", { name: "ro" }));
    expect(latest.volumes).toEqual([{ source: "data", target: "/data", readOnly: true }]);
    await user.click(screen.getByRole("button", { name: "Remove volume" }));

    // Environment: its own section, not a tab.
    expect(screen.queryByRole("tab", { name: /environment/i })).toBeNull();
    await user.click(screen.getByRole("button", { name: /add variable/i }));
    expect(screen.getByRole("textbox", { name: "Name of variable 1" })).toBeInTheDocument();
    await user.type(screen.getByPlaceholderText("KEY"), "MODE");
    await user.type(screen.getByPlaceholderText("value"), "prod");
    expect(latest.env).toEqual([{ key: "MODE", value: "prod" }]);
    await user.click(screen.getByRole("button", { name: "Remove env" }));

    // Networks
    await user.click(screen.getByRole("tab", { name: /networks/i }));
    await user.type(screen.getByPlaceholderText("network name"), "web{Enter}");
    expect(latest.networks).toEqual(["web"]);

    // Resources
    await user.click(screen.getByRole("tab", { name: /resources/i }));
    await user.type(screen.getByLabelText(/cpu limit/i), "2");
    await user.type(screen.getByLabelText(/memory limit/i), "512");
    await user.type(screen.getByLabelText(/memory reservation/i), "128");
    await user.type(screen.getByLabelText(/^gpus/i), "all");
    expect(latest).toMatchObject({ cpuLimit: 2, memoryLimitMB: 512, memoryReservationMB: 128, gpus: "all" });

    // Extra args
    await user.click(screen.getByRole("tab", { name: /extra args/i }));
    await user.type(screen.getByLabelText(/extra docker run arguments/i), "--init{Enter}--read-only");
    expect(containerSpecFormToSpec(latest).extraArgs).toEqual(["--init", "--read-only"]);
    void within;
  });
});
