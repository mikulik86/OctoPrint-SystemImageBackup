/*
 * System Image Backup - tab view model
 */
$(function () {
    var PLUGIN = "systemimagebackup";
    var WATCH_COMMAND = "/usr/local/sbin/octopi-image-backup watch"; // as on the tab

    function SystemImageBackupViewModel(parameters) {
        var self = this;
        self.loginState = parameters[0];

        self.isAdmin = ko.pureComputed(function () {
            return !!(self.loginState.isAdmin && self.loginState.isAdmin());
        });

        // ----------------------------------------------------------- state
        self.status = ko.observable(null);
        self.loading = ko.observable(false);
        self.saving = ko.observable(false);
        self.starting = ko.observable(false);
        self.waitingForStart = ko.observable(false);
        self.fresh = ko.observable(false);
        self.showLog = ko.observable(false);
        self.log = ko.observable("");
        self.install = {
            running: ko.observable(false),
            rc: ko.observable(null),
            output: ko.observable("")
        };
        self.installDismissed = ko.observable(false);

        // ---------------------------------------------------- settings form
        self.destination = ko.observable("");
        self.mode = ko.observable("update");
        self.imageName = ko.observable("");
        self.keep = ko.observable(3);
        self.extraMb = ko.observable(1024);
        self.stopServices = ko.observable("");
        self.showOnScreen = ko.observable(false);
        self.screenVt = ko.observable(8);
        self.allowAtCommand = ko.observable(true);
        self.destType = ko.observable("local");
        self.netShare = ko.observable("");
        self.netSubdir = ko.observable("");
        self.netOptions = ko.observable("");
        self.smbUser = ko.observable("");
        self.smbPassword = ko.observable("");
        self.savedSnapshot = ko.observable(null);
        self.testing = ko.observable(false);
        self.testResult = ko.observable(null);

        self.formData = ko.pureComputed(function () {
            return {
                destination: (self.destination() || "").trim(),
                mode: self.mode(),
                image_name: (self.imageName() || "").trim(),
                keep: parseInt(self.keep(), 10) || 0,
                extra_mb: parseInt(self.extraMb(), 10) || 0,
                stop_services: (self.stopServices() || "").trim(),
                show_on_screen: !!self.showOnScreen(),
                screen_vt: parseInt(self.screenVt(), 10) || 8,
                allow_atcommand: !!self.allowAtCommand(),
                dest_type: self.destType(),
                net_share: (self.netShare() || "").trim(),
                net_subdir: (self.netSubdir() || "").trim(),
                net_options: (self.netOptions() || "").trim(),
                smb_user: (self.smbUser() || "").trim()
            };
        });
        self.dirty = ko.pureComputed(function () {
            var saved = self.savedSnapshot();
            return (saved !== null && JSON.stringify(self.formData()) !== saved) || !!self.smbPassword();
        });
        // a test result belongs to the settings it was made with
        self.formJson = ko.pureComputed(function () {
            return JSON.stringify(self.formData());
        });
        self.formJson.subscribe(function () {
            self.testResult(null);
        });

        self.loadForm = function (s) {
            self.destination(s.destination || "");
            self.mode(s.mode || "update");
            self.imageName(s.image_name || "");
            self.keep(s.keep);
            self.extraMb(s.extra_mb);
            self.stopServices(s.stop_services || "");
            self.showOnScreen(!!s.show_on_screen);
            self.screenVt(s.screen_vt || 8);
            self.allowAtCommand(s.allow_atcommand !== false);
            self.destType(s.dest_type || "local");
            self.netShare(s.net_share || "");
            self.netSubdir(s.net_subdir || "");
            self.netOptions(s.net_options || "");
            self.smbUser(s.smb_user || "");
            self.smbPassword("");
            self.savedSnapshot(JSON.stringify(self.formData()));
        };

        self.revertSettings = function () {
            var st = self.status();
            if (st && st.settings) self.loadForm(st.settings);
        };

        self.logNamePreview = ko.pureComputed(function () {
            return (self.imageName() || "octopi-backup").replace(/\.img$/, "") + "-log.txt";
        });
        self.fileNamePreview = ko.pureComputed(function () {
            var name = (self.imageName() || "octopi-backup").replace(/\.img$/, "");
            return self.mode() === "new" ? name + "-YYYYMMDD-HHMMSS.img" : name + ".img";
        });

        // -------------------------------------------------- derived status
        self.helper = ko.pureComputed(function () {
            var st = self.status();
            return st && st.helper ? st.helper : null;
        });
        self.setupProblems = ko.pureComputed(function () {
            var h = self.helper();
            return h ? h.problems : [];
        });
        self.helperWarnings = ko.pureComputed(function () {
            var h = self.helper();
            return h && h.warnings ? h.warnings : [];
        });
        self.setupNeeded = ko.pureComputed(function () {
            return self.setupProblems().length > 0 || self.install.running();
        });
        self.helperInstalled = ko.pureComputed(function () {
            var h = self.helper();
            return !!(h && h.installed);
        });
        self.needsImageUtils = ko.pureComputed(function () {
            var h = self.helper();
            return !!(h && !h.image_backup);
        });
        self.canInstallFromUi = ko.pureComputed(function () {
            var h = self.helper();
            return !!(h && h.can_install_from_ui);
        });
        self.installCommand = ko.pureComputed(function () {
            var h = self.helper();
            return h ? h.install_command : "";
        });
        self.last = ko.pureComputed(function () {
            var st = self.status();
            return st ? st.last : null;
        });
        self.running = ko.pureComputed(function () {
            var st = self.status();
            return !!(st && st.running);
        });
        self.target = ko.pureComputed(function () {
            var st = self.status();
            return st ? st.target : null;
        });
        self.imageExists = ko.pureComputed(function () {
            var t = self.target();
            return !!(t && t.image_exists);
        });
        self.passwordSaved = ko.pureComputed(function () {
            var st = self.status();
            return !!(
                st &&
                st.smb_password_saved &&
                st.settings &&
                (self.smbUser() || "").trim() === (st.settings.smb_user || "")
            );
        });
        self.passwordMissing = ko.pureComputed(function () {
            return (
                self.destType() === "smb" &&
                (self.smbUser() || "").trim() !== "" &&
                !self.passwordSaved() &&
                !self.smbPassword()
            );
        });
        self.canTest = ko.pureComputed(function () {
            return (
                !self.testing() &&
                !self.running() &&
                !self.waitingForStart() &&
                !self.setupNeeded() &&
                !self.passwordMissing() &&
                !!(self.netShare() || "").trim()
            );
        });
        self.networkSummary = ko.pureComputed(function () {
            var t = self.target();
            if (!t || !t.network) return "";
            var text = "Connected when the backup starts";
            if (t.checked_at) {
                text +=
                    ". Last check " +
                    self.fmtDate(t.checked_at) +
                    ": " +
                    (t.free !== null ? self.fmtSize(t.free) + " free, " : "") +
                    (t.image_exists ? "image found" + (t.image_size ? " (" + self.fmtSize(t.image_size) + ")" : "") : "no image yet");
            }
            return text + ".";
        });
        self.testSummary = function (r) {
            var parts = ["Connected to " + r.location];
            if (r.free) parts.push(self.fmtSize(r.free) + " free");
            if (self.mode() === "new") {
                parts.push(r.images ? r.images + " image(s) there" : "no images yet");
            } else {
                parts.push(r.image_exists ? "image found (" + self.fmtSize(r.image_size) + ")" : "no image yet - the first backup creates it");
            }
            return parts.join(" · ") + ".";
        };
        self.suggestions = ko.pureComputed(function () {
            var st = self.status();
            if (!st || !st.detected_services) return [];
            var current = " " + (self.stopServices() || "").replace(/,/g, " ") + " ";
            return st.detected_services.filter(function (name) {
                var short = name.replace(/\.service$/, "");
                return current.indexOf(" " + name + " ") < 0 && current.indexOf(" " + short + " ") < 0;
            });
        });
        self.webAction = ko.pureComputed(function () {
            var st = self.status();
            return st && st.octodash ? st.octodash.web_action : "";
        });
        self.atCommand = ko.pureComputed(function () {
            var st = self.status();
            return st && st.octodash ? st.octodash.at_command : "@IMAGEBACKUP";
        });

        self.resultText = ko.pureComputed(function () {
            var l = self.last();
            if (!l) return "";
            return (
                {
                    success: "Success",
                    failed: "Failed",
                    interrupted: "Interrupted",
                    running: "Running"
                }[l.state] || l.state
            );
        });
        self.resultCss = ko.pureComputed(function () {
            var l = self.last();
            if (!l) return "";
            return (
                {
                    success: "label-success",
                    failed: "label-important",
                    interrupted: "label-warning",
                    running: "label-info"
                }[l.state] || ""
            );
        });

        self.startBlocker = ko.pureComputed(function () {
            var st = self.status();
            if (!st) return "Loading...";
            if (self.running() || self.waitingForStart()) return "A backup is running.";
            if (self.setupNeeded()) return "Finish the setup above first.";
            if (self.passwordMissing()) return "Enter the password for the SMB user first.";
            if (st.printer_busy) return "The printer is busy. Start the backup when the print has finished.";
            if (self.dirty()) return null; // checked again after saving
            if (st.target && !st.target.ok) return "Destination: " + st.target.message;
            return null;
        });
        self.canStart = ko.pureComputed(function () {
            return !self.starting() && !self.loading() && self.startBlocker() === null;
        });
        self.startHint = ko.pureComputed(function () {
            var blocker = self.startBlocker();
            if (blocker) return blocker;
            var t = self.target();
            var what;
            if (self.mode() === "new") {
                what = "Creates a new image.";
            } else if (t && t.image_exists && !self.fresh()) {
                what = "Updates the existing image" + (t.image_size ? " (" + self.fmtSize(t.image_size) + ")" : "") + ".";
            } else if (t && t.network && t.image_exists === null) {
                what = "Updates the image on the share, or creates it if it is not there yet.";
            } else {
                what = "Creates the image (the first run takes longest).";
            }
            return what + " OctoPrint is offline while the backup runs.";
        });

        // --------------------------------------------------------- helpers
        self.fmtDate = function (ts) {
            if (!ts) return "";
            if (typeof formatDate === "function") return formatDate(ts);
            return new Date(ts * 1000).toLocaleString();
        };
        self.fmtDuration = function (s) {
            if (!s && s !== 0) return "";
            var m = Math.floor(s / 60),
                sec = s % 60;
            if (m >= 60) return Math.floor(m / 60) + " h " + (m % 60) + " min";
            return m > 0 ? m + " min " + sec + " s" : sec + " s";
        };
        self.fmtSize = function (bytes) {
            if (bytes === null || bytes === undefined) return "";
            if (typeof formatSize === "function") return formatSize(bytes);
            return (bytes / 1e9).toFixed(1) + " GB";
        };
        self.notify = function (title, text, type) {
            new PNotify({
                title: title,
                text: text,
                text_escape: true,
                type: type || "info",
                hide: type !== "error"
            });
        };
        self.errorText = function (xhr) {
            if (xhr && xhr.responseJSON && xhr.responseJSON.error) return xhr.responseJSON.error;
            if (xhr && xhr.status === 403) return "Permission denied.";
            return "Request failed" + (xhr && xhr.status ? " (HTTP " + xhr.status + ")" : "") + ".";
        };

        self.copy = function (text) {
            if (!text) return;
            var done = function () {
                self.notify("Copied", "Copied to the clipboard.", "success");
            };
            if (navigator.clipboard && window.isSecureContext) {
                navigator.clipboard.writeText(text).then(done);
                return;
            }
            if (typeof copyToClipboard === "function") {
                copyToClipboard(text);
                done();
                return;
            }
            var ta = $("<textarea>").val(text).css({position: "fixed", top: 0, left: 0, opacity: 0}).appendTo("body");
            ta[0].select();
            try {
                document.execCommand("copy");
                done();
            } catch (e) {
                self.notify("Copy failed", "Select the text and copy it by hand.", "error");
            }
            ta.remove();
        };

        self.addService = function (name) {
            var current = (self.stopServices() || "").trim();
            self.stopServices((current ? current + " " : "") + name.replace(/\.service$/, ""));
        };

        // ------------------------------------------------------------ API
        self.applyStatus = function (st) {
            var wasDirty = self.dirty();
            self.status(st);
            self.log(st.log || "");
            if (st.install) {
                self.install.running(!!st.install.running);
                if (!self.installDismissed()) {
                    self.install.rc(st.install.rc);
                    self.install.output(st.install.output || "");
                }
            }
            if (!wasDirty && st.settings) self.loadForm(st.settings);
            if (st.running) self.waitingForStart(false);
            if (st.install && st.install.running) self.schedulePoll(2000);
        };

        self.refresh = function () {
            if (!self.isAdmin()) return $.Deferred().reject().promise();
            self.loading(true);
            return OctoPrint.simpleApiGet(PLUGIN)
                .done(self.applyStatus)
                .always(function () {
                    self.loading(false);
                });
        };

        self._pollTimer = null;
        self.schedulePoll = function (ms) {
            if (self._pollTimer) clearTimeout(self._pollTimer);
            self._pollTimer = setTimeout(function () {
                self._pollTimer = null;
                self.refresh();
            }, ms);
        };

        self.saveSettings = function () {
            if (!self.dirty()) return $.Deferred().resolve().promise();
            var data = self.formData();
            var password = self.smbPassword();
            var st = self.status();
            var hadPassword = !!(st && st.smb_password_saved);
            self.saving(true);
            return OctoPrint.settings
                .savePluginSettings(PLUGIN, data)
                .then(function () {
                    self.savedSnapshot(JSON.stringify(data));
                    if (data.dest_type === "smb" && data.smb_user && password) {
                        return OctoPrint.simpleApiCommand(PLUGIN, "set_credentials", {
                            user: data.smb_user,
                            password: password
                        });
                    }
                    if (data.dest_type === "smb" && !data.smb_user && hadPassword) {
                        return OctoPrint.simpleApiCommand(PLUGIN, "set_credentials", {user: "", password: ""});
                    }
                })
                .done(function () {
                    self.smbPassword("");
                    self.refresh();
                })
                .fail(function (xhr) {
                    self.notify("Settings not saved", self.errorText(xhr), "error");
                    self.refresh();
                })
                .always(function () {
                    self.saving(false);
                });
        };

        self.testDestination = function () {
            self.testing(true);
            self.saveSettings()
                .then(function () {
                    return OctoPrint.simpleApiCommand(PLUGIN, "test_destination", {});
                })
                .done(function (result) {
                    // refresh first: it may load normalised settings, which clears old results
                    self.refresh().always(function () {
                        self.testResult(result);
                    });
                })
                .fail(function (xhr) {
                    self.testResult({ok: false, message: self.errorText(xhr)});
                })
                .always(function () {
                    self.testing(false);
                });
        };

        self.startBackup = function () {
            var fresh = self.mode() === "update" && self.imageExists() && self.fresh();
            // OctoPrint puts the message into a <p> as HTML
            var question =
                "OctoPrint (and the services you listed) will stop now and start again when the backup is finished. " +
                "The web interface is unavailable in the meantime" +
                (self.showOnScreen() ? "; the Pi's screen shows the progress." : ".") +
                "</p><p>To watch the backup over SSH, run this on the Pi:<br>" +
                "<code>" + WATCH_COMMAND + "</code>";
            showConfirmationDialog({
                title: "Start system image backup?",
                message: question,
                question: fresh ? "The existing image will be replaced by a new one." : "",
                proceed: "Start backup",
                proceedClass: "primary",
                onproceed: function () {
                    self.starting(true);
                    self.saveSettings()
                        .then(function () {
                            return OctoPrint.simpleApiCommand(PLUGIN, "start", {fresh: fresh});
                        })
                        .done(function (resp) {
                            self.waitingForStart(true);
                            self.fresh(false);
                            self.notify("Backup started", resp.message, "info");
                            self.pollAfterStart(15);
                        })
                        .fail(function (xhr) {
                            self.notify("Backup not started", self.errorText(xhr), "error");
                            self.refresh();
                        })
                        .always(function () {
                            self.starting(false);
                        });
                }
            });
        };

        // The helper checks everything again as root before stopping OctoPrint. Poll a little
        // so a failure at that stage shows up here.
        self.pollAfterStart = function (n) {
            if (n <= 0) {
                self.waitingForStart(false);
                return;
            }
            setTimeout(function () {
                OctoPrint.simpleApiGet(PLUGIN).done(function (st) {
                    self.applyStatus(st);
                    if (st.last && st.last.state === "failed" && !st.running && self.waitingForStart()) {
                        // finished (or failed) before OctoPrint went down
                        if (st.last.started && Date.now() / 1000 - st.last.started < 120) {
                            self.waitingForStart(false);
                            self.notify("Backup failed", st.last.message, "error");
                            return;
                        }
                    }
                    self.pollAfterStart(n - 1);
                });
            }, 2000);
        };

        self.installHelper = function () {
            self.installDismissed(false);
            self.install.running(true);
            self.install.output("");
            self.install.rc(null);
            OctoPrint.simpleApiCommand(PLUGIN, "install_helper", {})
                .done(function () {
                    self.schedulePoll(2000);
                })
                .fail(function (xhr) {
                    self.install.running(false);
                    self.notify("Installation not started", self.errorText(xhr), "error");
                    self.refresh();
                });
        };

        self.dismissInstall = function () {
            self.installDismissed(true);
            self.install.output("");
            self.install.rc(null);
        };

        self.regenerateToken = function () {
            showConfirmationDialog({
                title: "Create a new OctoDash link?",
                message: "The current link stops working. Update the Custom Action in OctoDash afterwards.",
                proceed: "Create new link",
                onproceed: function () {
                    OctoPrint.simpleApiCommand(PLUGIN, "regenerate_token", {}).done(self.refresh);
                }
            });
        };

        // ------------------------------------------------------ callbacks
        self.onStartupComplete = function () {
            self.refresh();
        };
        self.onUserLoggedIn = function () {
            self.refresh();
        };
        self.onAfterTabChange = function (current) {
            if (current === "#tab_plugin_" + PLUGIN) self.refresh();
        };
        self.onServerReconnect = function () {
            self.waitingForStart(false);
            self.refresh();
        };
        self.onServerConnect = function () {
            self.refresh();
        };
        self.onEventSettingsUpdated = function () {
            if (!self.dirty()) self.refresh();
        };
        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin !== PLUGIN || !data) return;
            if (data.type === "install_finished") {
                self.refresh();
            } else if (data.type === "error") {
                self.notify("Backup not started", data.message, "error");
                self.refresh();
            }
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: SystemImageBackupViewModel,
        dependencies: ["loginStateViewModel"],
        elements: ["#tab_plugin_systemimagebackup"]
    });
});
