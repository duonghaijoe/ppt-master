// Build the tenant-scoped base for project API calls. Use this everywhere
// instead of the legacy `/api/projects/{name}/*` paths, which gate on
// membership of the `default` tenant and 403 on anything else.
export function projectApi(tenant: string, project: string): string {
  return `/api/tenants/${encodeURIComponent(tenant)}/projects/${encodeURIComponent(project)}`;
}
