/**
 * Icon set.
 *
 * A single inline-SVG sprite registry. Icons are 16×16 on a 1.5px stroke grid,
 * which keeps them legible at small sizes and visually consistent — a mixed set
 * of different stroke weights is the fastest way to make an interface look
 * assembled rather than designed.
 */

const PATHS = {
  home: 'M2.5 6.8L8 2.2l5.5 4.6V13a.8.8 0 01-.8.8H9.6V9.9H6.4v3.9H3.3a.8.8 0 01-.8-.8V6.8z',
  // Triangle, not a disc: the flat base is what reads as "play" at 26px, where a
  // circle with a triangle inside turns into a smudge.
  play: 'M5.6 3.4l6.6 4.6-6.6 4.6z',
  // Four corners pointing out, and the same shape pointing in. The convention
  // platform fullscreen buttons use: the icon shows what the control *does*, not
  // what the current state is.
  expand: 'M2.2 5.8V3.2a1 1 0 011-1h2.6M14 5.8V3.2a1 1 0 00-1-1h-2.6M2.2 10.2v2.6a1 1 0 001 1h2.6M14 10.2v2.6a1 1 0 01-1 1h-2.6',
  collapse: 'M5.8 2.2H3.2a1 1 0 00-1 1v2.6M10.2 2.2h2.6a1 1 0 011 1v2.6M5.8 13.8H3.2a1 1 0 01-1-1v-2.6M10.2 13.8h2.6a1 1 0 001-1v-2.6',
  pause: 'M5.6 3.4v9.2M10.4 3.4v9.2',
  // A microphone, not a generic dot: the recording button has to be
  // recognisable at 19px without a label.
  mic: 'M8 9.6a2 2 0 002-2v-3.2a2 2 0 00-4 0v3.2a2 2 0 002 2zM4.8 7.2a3.2 3.2 0 006.4 0M8 10.4v2.8',
  video: 'M2.2 5.4a1 1 0 011-1h5a1 1 0 011 1v5.2a1 1 0 01-1 1h-5a1 1 0 01-1-1zM9.2 8l4-2.6v5.2L9.2 8z',
  // A ring, for "record a circle". The dot inside is what distinguishes it from
  // the microphone button beside it at the same size.
  circle: 'M8 14.2A6.2 6.2 0 108 1.8a6.2 6.2 0 000 12.4zM8 6.4a1.6 1.6 0 100 3.2 1.6 1.6 0 000-3.2z',
  volume: 'M7.4 3.2L4 6H2.2v4H4l3.4 2.8zM10 6.2a2.4 2.4 0 010 3.6M11.9 4.2a5 5 0 010 7.6',
  volumeOff: 'M7.4 3.2L4 6H2.2v4H4l3.4 2.8zM10.2 6.6l3.4 2.8M13.6 6.6l-3.4 2.8',
  eye: 'M1.6 8s2.4-4.2 6.4-4.2S14.4 8 14.4 8 12 12.2 8 12.2 1.6 8 1.6 8zM8 9.8a1.8 1.8 0 100-3.6 1.8 1.8 0 000 3.6z',
  // Sliders, for the toggle that hides the chrome. Two lines and two knobs: at
  // 22px anything more detailed is a smudge.
  sliders: 'M2.4 4.6h4.4M9.6 4.6h4M2.4 11.4h4.4M9.6 11.4h4M8 2.8v3.6M8 9.6v3.6',
  search: 'M7.2 12.4a5.2 5.2 0 100-10.4 5.2 5.2 0 000 10.4zM11 11l3 3',
  bell: 'M8 1.8a4.2 4.2 0 00-4.2 4.2c0 3.1-1.2 4.1-1.2 4.1h10.8s-1.2-1-1.2-4.1A4.2 4.2 0 008 1.8zM6.6 12.4a1.6 1.6 0 002.8 0',
  smile: 'M8 14.2A6.2 6.2 0 108 1.8a6.2 6.2 0 000 12.4zM6 6.4v.1M10 6.4v.1M5.4 9.4a3.4 3.4 0 005.2 0',
  mail: 'M2.2 3.4h11.6v9.2H2.2zM2.2 3.8L8 8.2l5.8-4.4',
  heart: 'M8 13.6S2.4 10.3 2.4 6.4a2.9 2.9 0 015.6-1 2.9 2.9 0 015.6 1c0 3.9-5.6 7.2-5.6 7.2z',
  comment: 'M13.6 9.4a1.4 1.4 0 01-1.4 1.4H4.6L2.4 13V4a1.4 1.4 0 011.4-1.4h8.4A1.4 1.4 0 0113.6 4v5.4z',
  share: 'M11.4 5.4a1.9 1.9 0 100-3.8 1.9 1.9 0 000 3.8zM4.6 9.8a1.9 1.9 0 100-3.8 1.9 1.9 0 000 3.8zM11.4 14.4a1.9 1.9 0 100-3.8 1.9 1.9 0 000 3.8zM6.2 7.1l3.6-1.9M6.2 8.9l3.6 1.9',
  bookmark: 'M4 2.4h8v11.2L8 11.3l-4 2.3V2.4z',
  more: 'M3.4 8a.9.9 0 100-1.8.9.9 0 000 1.8zM8 8a.9.9 0 100-1.8.9.9 0 000 1.8zM12.6 8a.9.9 0 100-1.8.9.9 0 000 1.8z',
  plus: 'M8 3.2v9.6M3.2 8h9.6',
  close: 'M4 4l8 8M12 4l-8 8',
  check: 'M3.2 8.4l3.2 3.2 6.4-7.2',
  chevronRight: 'M6 3.2L10.8 8 6 12.8',
  chevronLeft: 'M10 3.2L5.2 8 10 12.8',
  chevronDown: 'M3.2 6L8 10.8 12.8 6',
  user: 'M8 8.2a2.8 2.8 0 100-5.6 2.8 2.8 0 000 5.6zM2.8 13.8a5.2 5.2 0 0110.4 0',
  users: 'M11 8.4a2.4 2.4 0 100-4.8 2.4 2.4 0 000 4.8zM3 13.6a4.2 4.2 0 018 0M6.2 3.8a2.4 2.4 0 010 4.6M13 13.6a4.2 4.2 0 00-1.6-3.3',
  settings: 'M8 10a2 2 0 100-4 2 2 0 000 4zM12.9 9.4a1.1 1.1 0 00.2 1.2l.1.1a1.3 1.3 0 11-1.9 1.9l-.1-.1a1.1 1.1 0 00-1.2-.2 1.1 1.1 0 00-.7 1v.2a1.3 1.3 0 11-2.6 0v-.1a1.1 1.1 0 00-.7-1 1.1 1.1 0 00-1.2.2l-.1.1a1.3 1.3 0 11-1.9-1.9l.1-.1a1.1 1.1 0 00.2-1.2 1.1 1.1 0 00-1-.7h-.2a1.3 1.3 0 110-2.6h.1a1.1 1.1 0 001-.7 1.1 1.1 0 00-.2-1.2l-.1-.1a1.3 1.3 0 111.9-1.9l.1.1a1.1 1.1 0 001.2.2h.1a1.1 1.1 0 00.7-1v-.2a1.3 1.3 0 112.6 0v.1a1.1 1.1 0 00.7 1 1.1 1.1 0 001.2-.2l.1-.1a1.3 1.3 0 111.9 1.9l-.1.1a1.1 1.1 0 00-.2 1.2v.1a1.1 1.1 0 001 .7h.2a1.3 1.3 0 110 2.6h-.1a1.1 1.1 0 00-1 .7z',
  logout: 'M6.2 13.4H3.6a.8.8 0 01-.8-.8V3.4a.8.8 0 01.8-.8h2.6M10.4 11l3-3-3-3M13.2 8H6',
  image: 'M13.8 9.6v2.6a.8.8 0 01-.8.8H3a.8.8 0 01-.8-.8V3.8a.8.8 0 01.8-.8h2.6M10.2 2.2h3.6v3.6M13.6 2.4L7.6 8.4',
  camera: 'M13.8 9.6v2.6a.8.8 0 01-.8.8H3a.8.8 0 01-.8-.8V3.8a.8.8 0 01.8-.8h2l1-1.6h3.2l1 1.6h2a.8.8 0 01.8.8v1.6M8 10a2.4 2.4 0 100-4.8A2.4 2.4 0 008 10z',
  send: 'M14.2 1.8L7.4 8.6M14.2 1.8L9.8 14.2 7.4 8.6 1.8 6.2 14.2 1.8z',
  trash: 'M2.6 4.2h10.8M6.2 4.2V2.8h3.6v1.4M4 4.2l.6 9a.8.8 0 00.8.8h5.2a.8.8 0 00.8-.8l.6-9M6.6 6.8v4.8M9.4 6.8v4.8',
  edit: 'M11.2 2.4l2.4 2.4-8.6 8.6-3.2.8.8-3.2 8.6-8.6zM9.6 4l2.4 2.4',
  shield: 'M8 14.4s5-2.2 5-6V3.8L8 2 3 3.8V8.4c0 3.8 5 6 5 6z',
  flag: 'M3.6 14V2.4M3.6 3h8.8l-1.6 2.8 1.6 2.8H3.6',
  eye: 'M1.6 8S3.9 3.6 8 3.6 14.4 8 14.4 8 12.1 12.4 8 12.4 1.6 8 1.6 8zM8 9.8a1.8 1.8 0 100-3.6 1.8 1.8 0 000 3.6z',
  eyeOff: 'M6.3 4a5.9 5.9 0 011.7-.4c4.1 0 6.4 4.4 6.4 4.4a11 11 0 01-2 2.5M4 5.2A11 11 0 001.6 8S3.9 12.4 8 12.4c.9 0 1.7-.2 2.4-.5M2 2l12 12M6.7 6.8a1.8 1.8 0 002.5 2.5',
  lock: 'M12.2 7.2H3.8a.8.8 0 00-.8.8v5a.8.8 0 00.8.8h8.4a.8.8 0 00.8-.8V8a.8.8 0 00-.8-.8zM5.4 7.2V5a2.6 2.6 0 015.2 0v2.2',
  link: 'M6.6 9.4a2.6 2.6 0 003.9.3l2-2a2.6 2.6 0 00-3.7-3.7l-1.1 1.1M9.4 6.6a2.6 2.6 0 00-3.9-.3l-2 2a2.6 2.6 0 003.7 3.7l1.1-1.1',
  location: 'M12.8 6.8c0 3.6-4.8 8-4.8 8s-4.8-4.4-4.8-8a4.8 4.8 0 019.6 0zM8 8.6a1.8 1.8 0 100-3.6 1.8 1.8 0 000 3.6z',
  calendar: 'M13.4 4.6H2.6v9h10.8v-9zM5.4 2.6v3M10.6 2.6v3M2.6 7.4h10.8',
  sparkle: 'M8 1.8l1.5 3.9 3.9 1.5-3.9 1.5L8 12.6 6.5 8.7 2.6 7.2l3.9-1.5L8 1.8z',
  info: 'M8 14.4A6.4 6.4 0 118 1.6a6.4 6.4 0 010 12.8zM8 7.2v4M8 4.6v.3',
  alert: 'M8 6.2v3.4M8 11.4v.3M7.2 2.4L1.8 12.2a1.1 1.1 0 001 1.7h10.4a1.1 1.1 0 001-1.7L8.8 2.4a1.1 1.1 0 00-1.6 0z',
  clock: 'M8 14.4A6.4 6.4 0 118 1.6a6.4 6.4 0 010 12.8zM8 4.6V8l2.2 1.4',
  refresh: 'M13.6 7.2A5.7 5.7 0 003.3 5.3M2.4 8.8a5.7 5.7 0 0010.3 1.9M13.9 3.2v4h-4M2.1 12.8v-4h4',
  copy: 'M5.6 5.6h6.8a.8.8 0 01.8.8v6.8a.8.8 0 01-.8.8H5.6a.8.8 0 01-.8-.8V6.4a.8.8 0 01.8-.8zM2.8 10.4H2.4a.8.8 0 01-.8-.8V2.8a.8.8 0 01.8-.8h6.8a.8.8 0 01.8.8v.4',
  globe: 'M8 14.4A6.4 6.4 0 118 1.6a6.4 6.4 0 010 12.8zM1.7 8h12.6M8 1.6a9.4 9.4 0 012 6.4 9.4 9.4 0 01-2 6.4A9.4 9.4 0 018 1.6z',
  file: 'M9.2 1.8H4.4a.8.8 0 00-.8.8v10.8a.8.8 0 00.8.8h7.2a.8.8 0 00.8-.8V5l-2.4-3.2zM9.2 1.8V5h3.2',
  download: 'M8 2.4v7.8M4.8 7.2L8 10.4l3.2-3.2M2.6 12.6h10.8',
  grid: 'M2.6 2.6h4.2v4.2H2.6zM9.2 2.6h4.2v4.2H9.2zM2.6 9.2h4.2v4.2H2.6zM9.2 9.2h4.2v4.2H9.2z',
  message: 'M2.4 3.6h11.2v7.2H6.6L3.4 13.2v-2.4H2.4V3.6z',
  hash: 'M5.8 2.2L4.2 13.8M10.6 2.2L9 13.8M2.4 5.6h11.2M2 10.4h11.2',
  verified: 'M8 1.6l1.6.9 1.8-.2.5 1.8 1.5 1-1 1.5.3 1.8-1.8.6-.9 1.6-1.6-.9-1.8.3-.5-1.8-1.5-1 1-1.5-.3-1.8 1.8-.6.9-1.6 1.6.9zM6.3 8.1l1.3 1.3 2.4-2.6',
  dots: 'M8 9.6a1.6 1.6 0 100-3.2 1.6 1.6 0 000 3.2z',
  at: 'M12 8v1.6a1.8 1.8 0 003.6 0V8a7.6 7.6 0 10-3.1 6.1M12 8v1.2a2 2 0 004 0V8a6 6 0 10-2.4 4.8',
  linkExternal: 'M9.4 2.6h4v4M13.4 2.6L7.6 8.4M12 9v3.4a.8.8 0 01-.8.8H3a.8.8 0 01-.8-.8V4.8a.8.8 0 01.8-.8h3.4',
  archive: 'M13.4 5.4H2.6M4.4 5.4V2.6h7.2v2.8M3.6 5.4v7.4a.8.8 0 00.8.8h7.2a.8.8 0 00.8-.8V5.4M6.6 8.2h2.8',
  ban: 'M13.4 8A5.4 5.4 0 112.6 8 5.4 5.4 0 0113.4 8zM4.4 4.4l7.2 7.2',
  bolt: 'M8.8 1.6L3.4 8.8h4l-.4 5.6 5.6-7.2h-4l.2-5.6z',
};

/**
 * Build an icon element.
 * @param {string} name  key from PATHS
 * @param {object} [options]  size, className, title
 */
export function icon(name, options = {}) {
  const { size = 16, className = 'icon', title = null, strokeWidth = 1.5 } = options;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', className);
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('fill', 'none');
  svg.setAttribute('aria-hidden', title ? 'false' : 'true');
  svg.setAttribute('role', title ? 'img' : 'presentation');

  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', PATHS[name] || PATHS.info);
  path.setAttribute('stroke', 'currentColor');
  path.setAttribute('stroke-width', String(strokeWidth));
  path.setAttribute('stroke-linecap', 'round');
  path.setAttribute('stroke-linejoin', 'round');
  svg.append(path);

  if (title) {
    const label = document.createElementNS('http://www.w3.org/2000/svg', 'title');
    label.textContent = title;
    svg.prepend(label);
  }
  return svg;
}

export const ICON_NAMES = Object.keys(PATHS);

/** Inline brand mark: two intertwining curves forming a leaf/heart. */
export function brandMark({ size = 26, className = '' } = {}) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 64 64');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('fill', 'none');
  svg.setAttribute('class', className);
  svg.setAttribute('aria-hidden', 'true');
  svg.innerHTML =
    '<path d="M32 5C44 17 44 35 30 59" stroke="#8d8479" stroke-width="4.5" stroke-linecap="round"/>' +
    '<path d="M32 5C20 17 20 35 34 59" stroke="#b3a794" stroke-width="4.5" stroke-linecap="round"/>';
  return svg;
}

export default icon;
