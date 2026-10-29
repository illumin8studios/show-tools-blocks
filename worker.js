// Illumin8 Show Tools - shared block submissions (Cloudflare Worker)
//
// The app POSTs {kind:"block", id, block, submitter, notes, spec_text, app_version} or {kind:"problem", ...} here.
// A block becomes a pull request adding submissions/<id>.json (the checks run on it and post the reviewer's report);
// a problem becomes an issue. The GitHub token stays here - the app never holds one.
//
// Settings (Workers > your worker > Settings > Variables):
//   GITHUB_TOKEN  secret: fine-grained token for the block repository only - Contents, Pull requests and Issues: read & write
//   REPO          e.g. illumin8studios/show-tools-blocks
//   LIMITS        optional KV namespace binding, for a per-address limit of 20 submissions a day

const MAX_BYTES = 200_000;

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (req.method !== "POST" || url.pathname !== "/submit") return json({ error: "POST /submit" }, 404);
    const len = Number(req.headers.get("content-length") || 0);
    if (len > MAX_BYTES) return json({ error: "too large" }, 413);
    let p;
    try { p = await req.json(); } catch { return json({ error: "not JSON" }, 400); }
    if (env.LIMITS) {
      const ip = req.headers.get("cf-connecting-ip") || "?";
      const key = `n:${ip}:${new Date().toISOString().slice(0, 10)}`;
      const n = Number((await env.LIMITS.get(key)) || 0);
      if (n >= 20) return json({ error: "daily limit reached - try tomorrow" }, 429);
      await env.LIMITS.put(key, String(n + 1), { expirationTtl: 90000 });
    }
    const gh = (path, method = "GET", body) => fetch(`https://api.github.com/repos/${env.REPO}${path}`, {
      method, body: body ? JSON.stringify(body) : undefined,
      headers: { Authorization: `Bearer ${env.GITHUB_TOKEN}`, "User-Agent": "illumin8-blocks", Accept: "application/vnd.github+json" },
    }).then(async r => ({ ok: r.ok, status: r.status, data: await r.json().catch(() => ({})) }));

    if (p.kind === "problem") {
      const r = await gh("/issues", "POST", {
        title: `Problem: ${clean(p.maker)} ${clean(p.model)}`.slice(0, 120),
        body: `**Block:** \`${clean(p.id)}\`\n**Reported by:** ${clean(p.submitter) || "anonymous"}\n\n${clean(p.text, 4000)}`,
        labels: ["problem report"],
      });
      return r.ok ? json({ url: r.data.html_url }) : json({ error: "couldn't open the issue" }, 502);
    }
    if (p.kind !== "block" || !p.block || !Array.isArray(p.block.sockets) || !p.block.model) return json({ error: "not a block" }, 400);
    const id = clean(p.id, 80).toLowerCase().replace(/[^a-z0-9-]/g, "-") || "block";
    const branch = `submit/${id}-${Date.now()}`;
    const main = await gh("/git/ref/heads/main");
    if (!main.ok) return json({ error: "repository not reachable" }, 502);
    const mk = await gh("/git/refs", "POST", { ref: `refs/heads/${branch}`, sha: main.data.object.sha });
    if (!mk.ok) return json({ error: "couldn't make the branch" }, 502);
    const path = `submissions/${id}.json`;
    const existing = await gh(`/contents/${path}?ref=main`);
    const file = { ...p, status: "shared" };
    const put = await gh(`/contents/${path}`, "PUT", {
      message: `Block: ${clean(p.block.maker)} ${clean(p.block.model)}`, branch,
      content: b64(JSON.stringify(file, null, 1)), sha: existing.ok ? existing.data.sha : undefined,
    });
    if (!put.ok) return json({ error: "couldn't add the file" }, 502);
    const pr = await gh("/pulls", "POST", {
      title: `${existing.ok ? "Update" : "New"} block: ${clean(p.block.maker)} ${clean(p.block.model)}`.slice(0, 120),
      head: branch, base: "main",
      body: `Submitted from Illumin8 Show Tools ${clean(p.app_version)} by ${clean(p.submitter) || "anonymous"}.\n\n` +
            (p.notes ? `> ${clean(p.notes, 2000)}\n\n` : "") +
            `Source: ${clean(p.block.source, 500)}\n\nThe automated checks post the review report below. To publish as ` +
            `**verified** (checked line by line against the manufacturer's spec), set \`"status": "verified"\` in the file before merging.`,
    });
    return pr.ok ? json({ url: pr.data.html_url }) : json({ error: "couldn't open the pull request" }, 502);
  },
};

function clean(s, n = 200) { return String(s ?? "").replace(/[\u0000-\u001f]/g, " ").slice(0, n); }
function b64(s) {
  const bytes = new TextEncoder().encode(s);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 8192) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
  return btoa(bin);
}
function json(o, status = 200) { return new Response(JSON.stringify(o), { status, headers: { "content-type": "application/json" } }); }
