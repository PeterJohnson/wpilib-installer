// Profile defaults are applied once; Qt's component page remains editable.
// The manager supplies component IDs from the trusted registry at launch.
function Controller() {}

Controller.prototype.ComponentSelectionPageCallback = function() {
    if (!installer.isInstaller() || this.profileApplied)
        return;
    var configured = installer.value("ProfileComponents", "");
    if (!configured)
        return;
    var components = JSON.parse(configured);
    var page = gui.currentPageWidget();
    page.deselectAll();
    for (var i = 0; i < components.length; ++i)
        page.selectComponent(components[i]);
    this.profileApplied = true;
};
