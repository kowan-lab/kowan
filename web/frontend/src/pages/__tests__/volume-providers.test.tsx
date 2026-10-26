// Volume providers beyond local/NFS: CIFS, tmpfs, the s3fs/sshfs plugins and a
// custom driver, each with its validation.
import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { requests } from "../../test/api";
import { pageRoot, renderApp } from "../../test/render";
import { choose, dialog, press, toast } from "../../test/ui";

async function open() {
  const r = renderApp("/volumes", { api: { "POST /hosts/:h/volumes": (q) => ({ name: q.body.name, driver: q.body.driver }) } });
  await within(pageRoot()).findByText("orphan-data", {}, { timeout: 10_000 });
  await press(r.user, /create volume/i, pageRoot());
  const d = await dialog(/create volume/i);
  await r.user.type(within(d).getByLabelText(/^name/i), "vol");
  return { ...r, d };
}

describe("volume providers", () => {
  it("CIFS needs server, share and an SMB credential", async () => {
    const { user, d } = await open();
    await choose(user, /provider/i, /cifs/i, d);
    await press(user, /^create$/i, d);
    await toast(/server is required/i);
    await user.type(within(d).getByLabelText(/^server/i), "files.example.com");
    await press(user, /^create$/i, d);
    await toast(/share is required/i);
    await user.type(within(d).getByLabelText(/^share/i), "media");
    await user.type(within(d).getByLabelText(/extra options/i), "vers=3.0");
    const cred = within(d).queryByRole("combobox", { name: /credential/i });
    if (cred && !/smb-user/.test(cred.textContent ?? "")) await choose(user, /credential/i, /smb-user/i, d);
    await press(user, /^create$/i, d);
    await toast(/volume created/i);
    expect(requests("POST", "/hosts/local/volumes")[0].body).toMatchObject({
      name: "vol", driver: "local", provider: "cifs", connectionRef: "c-smb",
      options: { type: "cifs", device: "//files.example.com/media", o: "vers=3.0" },
    });
  });

  it("tmpfs needs a size", async () => {
    const { user, d } = await open();
    await choose(user, /provider/i, /tmpfs/i, d);
    await press(user, /^create$/i, d);
    await toast(/size is required/i);
    await user.type(within(d).getByLabelText(/^size/i), "256m");
    await press(user, /^create$/i, d);
    await waitFor(() => expect(requests("POST", "/hosts/local/volumes")).toHaveLength(1));
    expect(requests("POST", "/hosts/local/volumes")[0].body).toMatchObject({ options: { type: "tmpfs", o: "size=256m" } });
  });

  it("s3fs installs the plugin and sends only non-secret options", async () => {
    const { user, d } = await open();
    await choose(user, /provider/i, /s3 \(s3fs\)/i, d);
    await press(user, /^create$/i, d);
    await toast(/bucket is required/i);
    await user.type(within(d).getByLabelText(/^bucket/i), "media");
    await user.type(within(d).getByLabelText(/endpoint url/i), "https://minio.example");
    expect(within(d).queryByLabelText(/secret access key/i)).toBeNull();
    const cred = within(d).queryByRole("combobox", { name: /credential/i });
    if (cred && !/aws-prod/.test(cred.textContent ?? "")) await choose(user, /credential/i, /aws-prod/i, d);
    await press(user, /^create$/i, d);
    await toast(/volume created/i);
    expect(requests("POST", "/hosts/local/plugins/install")[0].body).toMatchObject({ remote: "mochoa/s3fs-volume-plugin:latest" });
    expect(requests("POST", "/hosts/local/volumes")[0].body).toMatchObject({
      driver: "mochoa/s3fs-volume-plugin:latest", provider: "s3fs", connectionRef: "c-aws", options: { bucket: "media", url: "https://minio.example" },
    });
  });

  it("sshfs needs a host", async () => {
    const { user, d } = await open();
    await choose(user, /provider/i, /ssh \(sshfs\)/i, d);
    await press(user, /^create$/i, d);
    await toast(/host is required/i);
  });

  it("a custom driver takes free options", async () => {
    const { user, d } = await open();
    await choose(user, /provider/i, /custom driver/i, d);
    await press(user, /^create$/i, d);
    await toast(/driver is required/i);
    await user.type(within(d).getByLabelText(/^driver$/i), "rexray/ebs");
    await user.click(within(d).getAllByRole("button", { name: /^add/i })[0]);
    await user.type(within(d).getAllByPlaceholderText(/key|option/i)[0], "size");
    await user.type(within(d).getAllByPlaceholderText(/value/i)[0], "20");
    await press(user, /^create$/i, d);
    await waitFor(() => expect(requests("POST", "/hosts/local/volumes")).toHaveLength(1));
    expect(requests("POST", "/hosts/local/volumes")[0].body).toMatchObject({ driver: "rexray/ebs", options: { size: "20" } });
  });

  it("asks for a host in the all-hosts view", async () => {
    localStorage.setItem("kowan_host", "all");
    renderApp("/volumes");
    expect(await screen.findAllByText("orphan-data", {}, { timeout: 10_000 })).not.toHaveLength(0);
  });
});
