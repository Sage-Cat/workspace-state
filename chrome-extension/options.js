const defaults = {profile: 'Default', profileDirectory: 'Default', appId: 'google-chrome'};

async function load() {
    const config = {...defaults, ...await chrome.storage.local.get(defaults)};
    document.querySelector('#profile').value = config.profile;
    document.querySelector('#profileDirectory').value = config.profileDirectory;
    document.querySelector('#appId').value = config.appId;
}

async function save() {
    const profile = document.querySelector('#profile').value.trim() || defaults.profile;
    const profileDirectory = document.querySelector('#profileDirectory').value.trim() || defaults.profileDirectory;
    const appId = document.querySelector('#appId').value.trim() || defaults.appId;
    await chrome.storage.local.set({profile, profileDirectory, appId, profileConfigured: true});
    const status = document.querySelector('#status');
    status.textContent = 'Saved';
    setTimeout(() => { status.textContent = ''; }, 1500);
}

document.querySelector('#save').addEventListener('click', save);
load();
