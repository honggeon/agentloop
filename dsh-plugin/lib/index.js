/**
 * dsh-auditrail — Host side.
 *
 * Deliberately a no-op: all data flows through the auditrail FastAPI backend
 * (default http://127.0.0.1:18080), which the client bundle fetches directly.
 * The Host entry exists only because the Loader requires a module entry point.
 */
export function apply() {}
