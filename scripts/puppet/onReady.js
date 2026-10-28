// BackstopJS default onReady
module.exports = async (page, scenario, vp) => {
  await new Promise(r => setTimeout(r, 500));
};
