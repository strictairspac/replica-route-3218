import {RUN_STATES,CRASH_DECISIONS} from '../../constants/enums.js';
import {esc} from '../../utils/html.js';
export const badge=value=>`<span class="badge ${esc(value)}">${esc(RUN_STATES[value]||CRASH_DECISIONS[value]||value)}</span>`;
