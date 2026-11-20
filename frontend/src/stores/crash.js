import {crashApi} from '../api/crash.js';
export const crashStore={
  scripts:[],drills:[],
  async refresh(){
    const [scripts,drills]=await Promise.all([crashApi.listScripts(),crashApi.listDrills()]);
    this.scripts=scripts;this.drills=drills;return this;
  },
};
