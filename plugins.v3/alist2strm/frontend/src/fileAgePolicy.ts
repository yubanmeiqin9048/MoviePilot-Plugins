import type { FileAgePolicy } from '@/types'

export const DEFAULT_FILE_AGE_POLICY: FileAgePolicy = {
  recent_days: 30,
  cold_after_days: 90,
  archive_after_days: 365,
  stale_recheck_days: 7,
  cold_recheck_days: 30,
  archive_recheck_days: 90,
  uncertain_recheck_days: 7,
}
