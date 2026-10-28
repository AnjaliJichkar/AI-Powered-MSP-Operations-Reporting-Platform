/* Charts, drawn as inline SVG. No library, and there is not going to be one:
 * these are bars and there is no build step to hang a bundle off.
 *
 * Two rules decide how they look, and both come from the stylesheet's own
 * preamble — these pages are shown on a projector, to a customer, in a room:
 *
 *   - **The number is always printed.** A bar whose length is the only way to
 *     read it is a bar nobody can quote in a meeting, and a bar washed out by a
 *     beamer is no bar at all. Colour is never the only signal here either: a
 *     single-series chart uses one colour and says nothing by it, and the two
 *     series on the day chart are told apart by a hatch as well as a fill.
 *   - **Nothing is truncated.** Labels sit above their bars on a full-width
 *     line rather than in a column that would clip a long customer's name.
 *
 * Every chart also carries its numbers as text: a `<title>` per bar for a
 * pointer, an `aria-label` for a screen reader, and — for the one chart whose
 * figures are not printed on its bars — a table underneath, open to anybody.
 */

const CHART_INK = "#14181f";
const CHART_MUTED = "#545d6e";
const CHART_TRACK = "#eceff4";
const CHART_BAR = "#1a4f8a";
const CHART_BAR_SECOND = "#15653a";

/** A number a bar is drawn against: never zero, so a chart of all zeroes draws
 *  a row of empty tracks rather than dividing by nothing. */
function chartMax(values) {
  return Math.max(1, ...values);
}

function svgText(x, y, text, { anchor = "start", size = 13.5, weight = 400, fill = CHART_INK } = {}) {
  return `<text x="${x}" y="${y}" text-anchor="${anchor}" font-size="${size}" font-weight="${weight}" fill="${fill}">${escapeHtml(text)}</text>`;
}

/** A list of labelled bars: the label and its figure on one line, the bar under
 *  it. The label gets the whole width, so a long customer name wraps nothing
 *  and loses nothing. */
function barList(series, { total = null } = {}) {
  if (!series.length) {
    return '<p class="small muted">Nothing to draw: there are no open tickets.</p>';
  }
  const max = chartMax(series.map((s) => s.value));
  const rowHeight = 44;
  const barHeight = 10;
  const width = 480;
  const height = series.length * rowHeight;
  const bars = series
    .map((item, index) => {
      const top = index * rowHeight;
      const length = Math.max(2, Math.round((item.value / max) * width));
      const share = total ? ` (${Math.round((item.value / total) * 100)}% of the open tickets)` : "";
      const sentence = `${item.label}: ${item.value}${share}${item.note ? ` — ${item.note}` : ""}`;
      return `<g><title>${escapeHtml(sentence)}</title>
        ${svgText(0, top + 15, item.label, { weight: 600 })}
        ${svgText(width, top + 15, String(item.value), { anchor: "end", weight: 700 })}
        <rect x="0" y="${top + 24}" width="${width}" height="${barHeight}" rx="5" fill="${CHART_TRACK}"></rect>
        <rect x="0" y="${top + 24}" width="${length}" height="${barHeight}" rx="5" fill="${CHART_BAR}"></rect>
      </g>`;
    })
    .join("");
  const summary = series.map((s) => `${s.label} ${s.value}`).join(", ");
  /* No `height` attribute: the viewBox plus `height: auto` in the stylesheet
     makes the drawing scale with the box it is in, so the card is exactly as
     tall as its bars and the text grows on a wide screen rather than shrinking
     into a corner. */
  return `<svg viewBox="0 0 ${width} ${height}" role="img"
       aria-label="${escapeHtml(summary)}">${bars}</svg>`;
}

/** Two shares over a row of named slots — accuracy and coverage at each
 *  confidence threshold, as the Autopilot results page reads them.
 *
 *  Both numbers are printed above their own bar, because the whole point of the
 *  chart is that the two trade against each other and a reader has to be able
 *  to quote the pair at one threshold. The second series carries a hatch as
 *  well as its colour. One slot may be `marked`: the threshold a rule chose,
 *  which gets a caret and a bold label as well as its own colour, so what the
 *  mark means is readable without the legend.
 *
 *  `points` are `{ label, first, second }` with the shares as 0 to 1, or null
 *  where there is nothing to draw — a slot nobody answered prints "—" rather
 *  than a zero that reads like a measurement. */
function percentColumns(points, { marked = null } = {}) {
  if (!points.length) return '<p class="small muted">No thresholds to draw.</p>';
  const width = 960;
  const plot = 150;
  const top = 20;
  const axis = 40;
  const height = top + plot + axis;
  const slot = width / points.length;
  const barWidth = Math.max(4, slot / 2 - 6);
  const share = (value) => (value === null || value === undefined ? null : value);
  const asText = (value) => (value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}`);
  const columns = points
    .map((point, index) => {
      const left = index * slot;
      const first = share(point.first);
      const second = share(point.second);
      const firstHeight = Math.round((first || 0) * plot);
      const secondHeight = Math.round((second || 0) * plot);
      const isMarked = marked !== null && point.label === marked;
      const base = top + plot;
      return `<g><title>${escapeHtml(
        `${point.label}: accuracy ${asText(first)}%, coverage ${asText(second)}%${
          isMarked ? " — the threshold the rule chose" : ""
        }`
      )}</title>
        <rect x="${left + 2}" y="${base - firstHeight}" width="${barWidth}" height="${firstHeight}" fill="${CHART_BAR}"></rect>
        <rect x="${left + barWidth + 6}" y="${base - secondHeight}" width="${barWidth}" height="${secondHeight}" fill="${CHART_BAR_SECOND}"></rect>
        <rect x="${left + barWidth + 6}" y="${base - secondHeight}" width="${barWidth}" height="${secondHeight}" fill="url(#hatch-pc)"></rect>
        ${svgText(left + 2 + barWidth / 2, base - firstHeight - 5, asText(first), { anchor: "middle", size: 11 })}
        ${svgText(left + barWidth + 6 + barWidth / 2, base - secondHeight - 5, asText(second), { anchor: "middle", size: 11, fill: CHART_MUTED })}
        ${svgText(left + slot / 2, base + 16, point.label, {
          anchor: "middle",
          size: 12,
          weight: isMarked ? 700 : 400,
        })}
        ${
          isMarked
            ? svgText(left + slot / 2, base + 32, "▲ chosen", { anchor: "middle", size: 11, fill: CHART_BAR })
            : ""
        }
      </g>`;
    })
    .join("");
  const summary = points
    .map((point) => `${point.label}: accuracy ${asText(point.first)}%, coverage ${asText(point.second)}%`)
    .join("; ");
  return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(summary)}">
      <defs>
        <pattern id="hatch-pc" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <line x1="0" y1="0" x2="0" y2="5" stroke="#ffffff" stroke-width="2.4"></line>
        </pattern>
      </defs>
      <line x1="0" y1="${top + plot}" x2="${width}" y2="${top + plot}" stroke="${CHART_MUTED}" stroke-width="1"></line>
      ${columns}
    </svg>
    <div class="chart-legend">
      <span class="key"><span class="swatch created" aria-hidden="true"></span>Accuracy</span>
      <span class="key"><span class="swatch resolved" aria-hidden="true"></span>Coverage</span>
    </div>`;
}

/** Two series a day, over a fortnight. The bars are told apart by a hatch as
 *  well as a colour, and the figures are in the table underneath, so neither
 *  the colour nor a hover is ever the only way to read it. */
function dayColumns(days) {
  if (!days.length) return '<p class="small muted">No days to draw.</p>';
  const max = chartMax(days.flatMap((d) => [d.created, d.resolved]));
  /* Wider than the bar charts on purpose: this one is drawn across the whole
     card, and the drawing scales to the width it is given, so a square-ish
     viewBox would come out three hundred pixels tall. */
  const width = 960;
  const plot = 120;
  const axis = 18;
  const height = plot + axis;
  const slot = width / days.length;
  const barWidth = Math.max(3, slot / 2 - 2);
  const columns = days
    .map((day, index) => {
      const left = index * slot;
      const createdHeight = Math.round((day.created / max) * plot);
      const resolvedHeight = Math.round((day.resolved / max) * plot);
      const readable = new Date(`${day.day}T00:00:00Z`);
      const when = readable.toLocaleDateString(undefined, { day: "numeric", month: "short" });
      return `<g><title>${escapeHtml(`${when}: ${day.created} created, ${day.resolved} resolved`)}</title>
        <rect x="${left + 1}" y="${plot - createdHeight}" width="${barWidth}" height="${createdHeight}" fill="${CHART_BAR}"></rect>
        <rect x="${left + barWidth + 3}" y="${plot - resolvedHeight}" width="${barWidth}" height="${resolvedHeight}" fill="${CHART_BAR_SECOND}"></rect>
        <rect x="${left + barWidth + 3}" y="${plot - resolvedHeight}" width="${barWidth}" height="${resolvedHeight}" fill="url(#hatch)"></rect>
        ${svgText(left + slot / 2, plot + 14, String(readable.getUTCDate()), { anchor: "middle", size: 11, fill: CHART_MUTED })}
      </g>`;
    })
    .join("");
  const summary = days.map((d) => `${d.day}: ${d.created} created, ${d.resolved} resolved`).join("; ");
  const rows = days
    .map(
      (d) => `<tr><td class="nowrap">${escapeHtml(d.day)}</td><td class="num">${d.created}</td><td class="num">${d.resolved}</td></tr>`,
    )
    .join("");
  return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(summary)}">
      <defs>
        <pattern id="hatch" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <line x1="0" y1="0" x2="0" y2="5" stroke="#ffffff" stroke-width="2.4"></line>
        </pattern>
      </defs>
      <line x1="0" y1="${plot}" x2="${width}" y2="${plot}" stroke="${CHART_MUTED}" stroke-width="1"></line>
      ${columns}
    </svg>
    <div class="chart-legend">
      <span class="key"><span class="swatch created" aria-hidden="true"></span>Created</span>
      <span class="key"><span class="swatch resolved" aria-hidden="true"></span>Resolved</span>
    </div>
    <details class="detail-box chart-figures">
      <summary>The figures</summary>
      <div class="scroller"><table>
        <thead><tr><th>Day</th><th>Created</th><th>Resolved</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
    </details>`;
}
