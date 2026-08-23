// The standalone shell is intentionally dev-only; federation exposes only AppPage and Config.
if (import.meta.env.DEV) {
  void import('./dev/bootstrap')
}
