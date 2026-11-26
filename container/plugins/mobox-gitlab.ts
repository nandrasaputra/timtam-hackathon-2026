import { execFile } from "node:child_process"
import { promisify } from "node:util"
import { Plugin } from "@opencode/plugin"

const exec = promisify(execFile)
const operations = ["search", "branches", "file", "mrs", "mr", "diff", "pipelines", "trace", "issues", "commits", "crash"]
const projects = ["gohost", "ios"]

function searchResults(stdout: string): string {
  const hits = JSON.parse(stdout) as { path: string; startline: number; data: string }[]
  if (!hits.length) return "No results."
  return JSON.stringify(hits.slice(0, 10).map(({ path, startline, data }) => ({
    path,
    line: startline,
    snippet: data.slice(0, 200),
  })))
}

function fileExcerpt(stdout: string, start: number): string {
  const lines = stdout.split("\n")
  const excerpt = lines.slice(start - 1, start + 79).join("\n").slice(0, 5000)
  return `Excerpt starting at line ${start} of ${lines.length}\n${excerpt}`
}

export default Plugin.define({
  id: "mobox-gitlab",
  async setup(ctx) {
    await ctx.tool.transform((editor) => {
      editor.add({
        name: "mobox_gitlab",
        description:
          "Run the read-only mobox-glab wrapper against GitLab. Use project gohost for Android and ios for iOS. " +
          "For branches pass [partial_branch] to find matching branch names on that project. " +
          "For search pass [branch, term]. For file pass [branch, path] and optionally set start_line for an excerpt. For mr or diff pass [iid]. " +
          "For mrs pass [state], pipelines [branch], trace [job_id], issues [label] or [], " +
          "commits [branch, path], and crash []. Repository paths are on GitLab.",
        options: { codemode: false },
        input: {
          type: "object",
          properties: {
            operation: { type: "string", enum: operations },
            project: { type: "string", enum: projects },
            args: { type: "array", items: { type: "string" }, maxItems: 2 },
            start_line: { type: "integer", minimum: 1, description: "First line of a file excerpt. Defaults to 1." },
          },
          required: ["operation", "project", "args"],
          additionalProperties: false,
        },
        async execute(input) {
          const { operation, project, args, start_line = 1 } = input as {
            operation: string
            project: string
            args: string[]
            start_line?: number
          }
          if (!operations.includes(operation) || !projects.includes(project)) {
            throw new Error("Choose a supported GitLab operation and project.")
          }
          try {
            const { stdout } = await exec("/usr/local/bin/mobox-glab", [operation, project, ...args], {
              timeout: 45_000,
              maxBuffer: 2_000_000,
              env: { ...process.env, GLAB_SEND_TELEMETRY: "false", GLAB_CHECK_UPDATE: "false" },
            })
            if (operation === "search") return { content: searchResults(stdout) }
            if (operation === "file") return { content: fileExcerpt(stdout, start_line) }
            if (operation === "branches") {
              const matches = stdout.trim().split("\n").filter(Boolean)
              if (!matches.length) return { content: "No matching branches." }
              const names = matches.join("\n")
              if (matches.includes(args[0])) return { content: `Matching branches:\n${names}` }
              return {
                content: `No exact match for "${args[0]}" on ${project}. Matching branch names:\n${names}\n\n` +
                  "If an exact branch was requested, report the missing branch and suggest these names in your reply. " +
                  "Ask which branch to use and end the lookup for this project until the user replies.",
              }
            }
            return { content: stdout.trim().slice(0, 5000) || "No results." }
          } catch (error) {
            const failed = error as { stderr?: string }
            const reason = failed.stderr?.trim().slice(-1200) || "The GitLab lookup failed."
            throw new Error(reason)
          }
        },
      })
    })
  },
})
