/**
 * Pure utility functions — no DOM, no fetch, no globals.
 * Mirrored from the inline script in index.html and covered by
 * tests/js/utils.test.js.
 */

/**
 * Formats elapsed milliseconds into a human-readable uptime string.
 * @param {number} ms — elapsed milliseconds (>= 0)
 * @returns {string} — e.g. "2d 3h" or "1h 45m"
 */
export function formatUptime(ms) {
  const d = Math.floor(ms / 86400000);
  const h = Math.floor((ms % 86400000) / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  return d > 0 ? `${d}d ${h}h` : `${h}h ${m}m`;
}

/**
 * Formats a listening session duration in seconds.
 * @param {number} seconds — total elapsed seconds
 * @returns {string} — e.g. "4:07 listening" or "1h 23m listening"
 */
export function formatSessionTime(seconds) {
  const h   = Math.floor(seconds / 3600);
  const m   = Math.floor((seconds % 3600) / 60);
  const sec = seconds % 60;
  return h > 0
    ? `${h}h ${m}m listening`
    : `${m}:${String(sec).padStart(2, "0")} listening`;
}

/**
 * Parses a MIME-style server_type string into an uppercase format label.
 * @param {string} serverType — e.g. "audio/mpeg"
 * @returns {string} — e.g. "MPEG" or "—" if unparseable
 */
export function parseMountPoint(serverType) {
  if (!serverType || typeof serverType !== "string") return "—";
  const part = serverType.split("/")[1];
  return part ? part.toUpperCase() : "—";
}

/**
 * Formats a play count as an English ordinal — powers the "heard this before"
 * badge ("3rd play"). The 11/12/13 exception is why this isn't just a lookup
 * on the last digit: 13 is "13th", not "13rd".
 * @param {number} n — a positive integer
 * @returns {string} — e.g. "1st", "2nd", "3rd", "4th", "13th", "21st"
 */
export function ordinal(n) {
  const teens = n % 100;
  if (teens >= 11 && teens <= 13) return `${n}th`;
  return `${n}${["th", "st", "nd", "rd"][n % 10] || "th"}`;
}

/**
 * Builds the Genius search URL for a track — the hero title links to it.
 * Genius has no keyless "page for this song" endpoint, so this is its search
 * page with the query pre-filled. Version suffixes are dropped ("Closer
 * (Extended Mix)" -> "Closer") because Genius indexes songs, not releases, and
 * the remix name is exactly the token that makes the search miss. Only the
 * primary artist is used: a full "A x B x C" credit returns no song at all.
 * @param {string} artist
 * @param {string} title
 * @returns {string} — the URL, or "" when there is no song to link to
 */
const GENIUS_ARTIST_SEP = /\s+(?:x|vs\.?|feat\.?|ft\.?|featuring|with)\s+|,\s*/i;

export function geniusSearchUrl(artist, title) {
  const track = String(title || "").replace(/[([][^)\]]*[)\]]/g, " ").split(" - ")[0].trim();
  if (!track) return "";
  // Primary artist only — a full "A x B x C" credit makes Genius's search miss.
  const who = String(artist || "").split(GENIUS_ARTIST_SEP)[0].trim();
  const q = `${who} ${track}`.replace(/\s+/g, " ").trim();
  return `https://genius.com/search?q=${encodeURIComponent(q)}`;
}
