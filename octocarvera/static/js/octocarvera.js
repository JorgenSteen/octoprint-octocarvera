/**
 * OctoCarveraViewModel — Knockout view model for the OctoCarvera plugin.
 *
 * Drives the Carvera tab, sidebar panels (status, files, transfer), and
 * settings. Receives real-time machine state from the backend via
 * OctoPrint's plugin message bus (onDataUpdaterPluginMessage) and sends
 * commands via the SimpleAPI.
 *
 * Key sections:
 *   - Observables: machine state, positions, feed/spindle, jog, files
 *   - Button logic: activity-driven enable/disable (mirrors backend)
 *   - XY Knob: canvas-based proportional joystick for XY jogging
 *   - SD Card: file browser, upload, delete, move, create folder
 *   - Firmware flash: settings-panel UI (plain DOM, not Knockout-bound)
 *   - Status updates: processes backend status messages into observables
 */
$(function () {
    function OctoCarveraViewModel(parameters) {
        var self = this;

        self.settingsViewModel = parameters[0];

        // === Machine State ===
        self.state = ko.observable("Unknown");
        self.activity = ko.observable("unknown");
        self.connected = ko.observable(false);
        self.allowedActions = ko.observableArray([]);

        // === Positions ===
        // Machine position (MPos): absolute coordinates from machine home.
        // Work position (WPos): offset by the active work coordinate system.
        self.machineX = ko.observable("0.000");
        self.machineY = ko.observable("0.000");
        self.machineZ = ko.observable("0.000");
        self.machineA = ko.observable("0.000");
        self.machineB = ko.observable("0.000");
        self.workX = ko.observable("0.000");
        self.workY = ko.observable("0.000");
        self.workZ = ko.observable("0.000");
        self.workA = ko.observable("0.000");
        self.workB = ko.observable("0.000");

        // === Feed / Spindle ===
        self.feedCurrent = ko.observable("0");
        self.feedMax = ko.observable("0");
        self.feedOverride = ko.observable(100);
        self.spindleCurrent = ko.observable("0");
        self.spindleMax = ko.observable("0");
        self.spindleOverride = ko.observable(100);
        self.spindleOn = ko.observable(false);
        self.spindleTemp = ko.observable("--");

        // === Tool / Versions ===
        self.toolNumber = ko.observable("--");
        // Map raw tool numbers to human-friendly names.
        // T0=Probe, T1-T100=cutting tools, T8888=Laser, T999990+=3D Probe.
        self.toolDisplayName = ko.computed(function () {
            var raw = self.toolNumber();
            if (raw === "--" || raw === "-") return raw;
            var t = parseInt(raw, 10);
            if (isNaN(t) || t < 0) return "Empty";
            if (t === 0) return "Probe";
            if (t >= 1 && t <= 100) return "T" + t;
            if (t === 8888) return "Laser";
            if (t >= 999990 && t <= 999999) return "3D Probe";
            return "T" + t;
        });
        self.firmwareVersion = ko.observable(null);
        self.pluginVersion = ko.observable(null);

        // Jog step: discrete button selection
        self.jogSteps = ko.observableArray([0.1, 1, 5, 10, 20]);
        self.jogStep = ko.observable(1);
        self.setJogStep = function (step) { self.jogStep(step); };

        // Jog lock (default: locked to prevent accidental movement)
        self.jogLocked = ko.observable(true);
        self.toggleJogLock = function () { self.jogLocked(!self.jogLocked()); };

        // === Extra Status Fields ===
        self.haltReason = ko.observable(null);
        self.wpVoltage = ko.observable("0.00");
        self.laserMode = ko.observable(0);
        self.laserPower = ko.observable("0.0");
        self.laserScale = ko.observable("100.0");
        self.machineModel = ko.observable("");
        self.feedOverrideDisplay = ko.observable("100");
        self.spindleOverrideDisplay = ko.observable("100");
        self.playbackPercent = ko.observable(null);
        self.playbackElapsed = ko.observable(null);

        // === Go To ===
        // Each axis has a goto observable (editable field) and a _lastWPos
        // tracker. When a status update arrives, goto is only updated if
        // the field still matches _lastWPos — meaning the user hasn't
        // manually edited it. This prevents machine updates from clobbering
        // a coordinate the user typed in but hasn't sent yet.
        self._lastWPosX = "0.000";
        self._lastWPosY = "0.000";
        self._lastWPosZ = "0.000";
        self._lastWPosA = "0.000";
        self.gotoX = ko.observable("0.000");
        self.gotoY = ko.observable("0.000");
        self.gotoZ = ko.observable("0.000");
        self.gotoA = ko.observable("0.000");
        self.gotoStep = ko.observable("1.000");

        self.nudgeCoord = function (axis, direction) {
            var obs = {X: self.gotoX, Y: self.gotoY, Z: self.gotoZ, A: self.gotoA}[axis];
            if (!obs) return;
            var val = parseFloat(obs()) || 0;
            var step = parseFloat(self.gotoStep()) || 1;
            var result = val + (step * direction);
            obs(result.toFixed(3));
        };

        // Webcam
        self.webcamUrl = ko.observable("/webcam/?action=stream");

        // SD Card file browser
        self.sdFiles = ko.observableArray([]);
        self.sdPath = ko.observable("/sd/gcodes");
        self.sdLoading = ko.observable(false);
        self.sdError = ko.observable(null);
        self.octoprintFiles = ko.observableArray([]);
        self.selectedUploadFile = ko.observable(null);

        // File manager UI state
        self.sdSearchQuery = ko.observable("");
        self.sdSortBy = ko.observable("name");
        self.sdSortAsc = ko.observable(true);
        self.sdListStyle = ko.observable("folders_files");

        // Computed: filtered by search query, sorted by name/size/date,
        // with configurable folder grouping (folders first, files first, or mixed).
        self.filteredSdFiles = ko.computed(function () {
            var files = self.sdFiles();
            var query = (self.sdSearchQuery() || "").toLowerCase();
            var sortBy = self.sdSortBy();
            var asc = self.sdSortAsc();
            var style = self.sdListStyle();

            // Filter by search query
            if (query) {
                files = files.filter(function (f) {
                    return f.name.toLowerCase().indexOf(query) >= 0;
                });
            }

            // Sort
            files = files.slice().sort(function (a, b) {
                // Handle folder grouping
                if (style === "folders_files") {
                    if (a.is_dir && !b.is_dir) return -1;
                    if (!a.is_dir && b.is_dir) return 1;
                } else if (style === "files_folders") {
                    if (a.is_dir && !b.is_dir) return 1;
                    if (!a.is_dir && b.is_dir) return -1;
                }

                var valA, valB;
                if (sortBy === "name") {
                    valA = a.name.toLowerCase();
                    valB = b.name.toLowerCase();
                } else if (sortBy === "size") {
                    valA = a.size || 0;
                    valB = b.size || 0;
                } else if (sortBy === "date") {
                    valA = a.date || "";
                    valB = b.date || "";
                }

                if (valA < valB) return asc ? -1 : 1;
                if (valA > valB) return asc ? 1 : -1;
                return 0;
            });

            return files;
        });

        // Upload progress
        self.uploadInProgress = ko.observable(false);
        self.uploadFilename = ko.observable("");
        self.uploadPercent = ko.observable(0);
        self.uploadEta = ko.observable(null);
        self.uploadSpeed = ko.observable(null);

        // Estimated serial transfer rate (~1.3 KB/s for XMODEM-128 at 115200)
        self._XMODEM_RATE = 1.3 * 1024; // bytes per second

        // State CSS
        self.stateClass = ko.computed(function () {
            switch (self.state()) {
                case "Idle": return "octocarvera-state-idle";
                case "Run": return "octocarvera-state-run";
                case "Hold": case "Pause": case "Wait": return "octocarvera-state-hold";
                case "Tool": return "octocarvera-state-tool";
                case "Alarm": return "octocarvera-state-alarm";
                case "Home": return "octocarvera-state-home";
                default: return "octocarvera-state-unknown";
            }
        });

        // Button enables — activity-driven (see backend _compute_activity).
        // "idle"        : machine at rest, everything available
        // "jogging"     : mid-motion (single jog running) — only jog buttons
        //                 + spindle OFF stay live. Further jogs queue in a
        //                 one-slot buffer on the plugin side (latest wins).
        // "running_job" : OctoPrint streaming a file — only pause/cancel/overrides
        // "paused"      : paused job — only resume/cancel/overrides
        // "alarm"       : only unlock/estop/restart
        self.canJog = ko.computed(function () {
            var a = self.activity();
            return a === "idle" || a === "jogging";
        });
        // Goto and navigation presets only fire from Idle — they're longer
        // moves and shouldn't queue up.
        self.canGoto = ko.computed(function () { return self.activity() === "idle"; });
        // Spindle ON requires a valid cutting tool (T1–T100). Probes (T0,
        // T999990) and "no tool" (negative/garbage values) must not spin.
        self.spindleAllowed = ko.computed(function () {
            var t = parseInt(self.toolNumber(), 10);
            return !isNaN(t) && t >= 1 && t <= 100;
        });
        // Starting the spindle requires the machine to be at rest AND a valid tool.
        self.canSpindle = ko.computed(function () { return self.activity() === "idle" && self.spindleAllowed(); });
        // Stopping the spindle is a safety path: live whenever the user is in
        // control (idle or user-jogging). During a streamed job / paused
        // state, use E-stop instead.
        self.canSpindleOff = ko.computed(function () {
            var a = self.activity();
            return a === "idle" || a === "jogging";
        });
        self.canPause = ko.computed(function () { return self.activity() === "running_job"; });
        self.canResume = ko.computed(function () { return self.activity() === "paused"; });
        self.canCancel = ko.computed(function () {
            var a = self.activity();
            return a === "running_job" || a === "paused";
        });
        self.canUnlock = ko.computed(function () { return self.activity() === "alarm"; });
        self.canRestart = ko.computed(function () { return self.allowedActions().indexOf("restart") >= 0; });

        // During a job (running or paused), lock all motion controls and
        // prevent the user from toggling the jog lock.
        self.jobActive = ko.computed(function () {
            var a = self.activity();
            return a === "running_job" || a === "paused";
        });

        // Jog gray-out: locked during active job, jog lock, or wrong state.
        self.motionBlocked = ko.computed(function () {
            if (self.jobActive()) return true;
            return self.jogLocked() || !self.canJog();
        });
        self.motionAllowed = ko.computed(function () { return !self.motionBlocked(); });

        // Goto gray-out: locked during active job, jog lock, or not idle.
        self.gotoAllowed = ko.computed(function () {
            if (self.jobActive()) return false;
            return !self.jogLocked() && self.canGoto();
        });

        // === Actions ===
        self.estop = function () { OctoPrint.simpleApiCommand("octocarvera", "estop"); };
        self.unlock = function () { OctoPrint.simpleApiCommand("octocarvera", "unlock"); };
        self.pause = function () { OctoPrint.simpleApiCommand("octocarvera", "job_pause"); };
        self.resume = function () { OctoPrint.simpleApiCommand("octocarvera", "job_resume"); };
        self.cancel = function () { if (confirm("Cancel job?")) OctoPrint.simpleApiCommand("octocarvera", "job_cancel"); };
        self.restartMachine = function () { if (confirm("Restart Carvera?")) OctoPrint.simpleApiCommand("octocarvera", "restart"); };

        // Navigation — Idle-only (no chaining, no mid-jog queuing)
        self.gotoClearance = function () { if (!self.gotoAllowed()) return; OctoPrint.simpleApiCommand("octocarvera", "goto_clearance"); };
        self.gotoWorkOrigin = function () { if (!self.gotoAllowed()) return; OctoPrint.simpleApiCommand("octocarvera", "goto_work_origin"); };
        self.gotoAnchor1 = function () { if (!self.gotoAllowed()) return; OctoPrint.simpleApiCommand("octocarvera", "goto_anchor1"); };
        self.gotoAnchor2 = function () { if (!self.gotoAllowed()) return; OctoPrint.simpleApiCommand("octocarvera", "goto_anchor2"); };

        // Spindle
        self.spindleOn = function () {
            if (self.jogLocked() || !self.canSpindle()) return;
            OctoPrint.simpleApiCommand("octocarvera", "spindle_on", {rpm: 10000});
        };
        // Spindle OFF is a safety path: stays live whenever the backend
        // says it's allowed, ignoring jogLocked/motionBlocked entirely.
        self.spindleOff = function () {
            if (!self.canSpindleOff()) return;
            OctoPrint.simpleApiCommand("octocarvera", "spindle_off");
        };

        // Overrides
        self._feedSliderActive = false;
        self._spindleSliderActive = false;
        self.setFeedOverride = function () { OctoPrint.simpleApiCommand("octocarvera", "feed_override", {value: self.feedOverride()}); };
        self.setSpindleOverride = function () { OctoPrint.simpleApiCommand("octocarvera", "spindle_override", {value: self.spindleOverride()}); };

        // === Jog Direction Buttons ===
        self.jogXPlus = function () { if (self.motionBlocked()) return; OctoPrint.simpleApiCommand("octocarvera", "jog", {x: self.jogStep(), y: 0, z: 0}); };
        self.jogXMinus = function () { if (self.motionBlocked()) return; OctoPrint.simpleApiCommand("octocarvera", "jog", {x: -self.jogStep(), y: 0, z: 0}); };
        self.jogYPlus = function () { if (self.motionBlocked()) return; OctoPrint.simpleApiCommand("octocarvera", "jog", {x: 0, y: self.jogStep(), z: 0}); };
        self.jogYMinus = function () { if (self.motionBlocked()) return; OctoPrint.simpleApiCommand("octocarvera", "jog", {x: 0, y: -self.jogStep(), z: 0}); };
        self.jogZUp = function () { if (self.motionBlocked()) return; OctoPrint.simpleApiCommand("octocarvera", "jog", {x: 0, y: 0, z: self.jogStep()}); };
        self.jogZDown = function () { if (self.motionBlocked()) return; OctoPrint.simpleApiCommand("octocarvera", "jog", {x: 0, y: 0, z: -self.jogStep()}); };

        // === Go To — absolute WPos coordinates ===
        self.goToX = function () {
            if (!self.gotoAllowed()) return;
            OctoPrint.simpleApiCommand("octocarvera", "goto", {x: parseFloat(self.gotoX())});
            self._lastWPosX = self.gotoX();
        };
        self.goToY = function () {
            if (!self.gotoAllowed()) return;
            OctoPrint.simpleApiCommand("octocarvera", "goto", {y: parseFloat(self.gotoY())});
            self._lastWPosY = self.gotoY();
        };
        self.goToZ = function () {
            if (!self.gotoAllowed()) return;
            OctoPrint.simpleApiCommand("octocarvera", "goto", {z: parseFloat(self.gotoZ())});
            self._lastWPosZ = self.gotoZ();
        };
        self.goToA = function () {
            if (!self.gotoAllowed()) return;
            OctoPrint.simpleApiCommand("octocarvera", "goto", {a: parseFloat(self.gotoA())});
            self._lastWPosA = self.gotoA();
        };
        self.goToAll = function () {
            if (!self.gotoAllowed()) return;
            OctoPrint.simpleApiCommand("octocarvera", "goto", {
                x: parseFloat(self.gotoX()),
                y: parseFloat(self.gotoY()),
                z: parseFloat(self.gotoZ()),
                a: parseFloat(self.gotoA())
            });
            self._lastWPosX = self.gotoX();
            self._lastWPosY = self.gotoY();
            self._lastWPosZ = self.gotoZ();
            self._lastWPosA = self.gotoA();
        };
        self.refreshGotoPositions = function () {
            self.gotoX(self.workX()); self._lastWPosX = self.workX();
            self.gotoY(self.workY()); self._lastWPosY = self.workY();
            self.gotoZ(self.workZ()); self._lastWPosZ = self.workZ();
            self.gotoA(self.workA()); self._lastWPosA = self.workA();
        };

        // === XY Knob — continuous proportional jog ===
        // Works like a joystick: direction from deflection, speed from amount
        // Sends G1 moves with feed rate proportional to deflection²
        // Each move is timed to complete in one interval → smooth continuous motion
        self._knobDragging = false;
        self._knobLastSend = 0;
        self._knobSendInterval = 300;  // ms between G1 sends (smooth motion without flooding serial)
        self._knobMaxFeed = 3000;      // mm/min at full deflection (Carvera Air rapid is 3000)
        self._knobTimer = null;
        self._KNOB_DEADZONE = 0.05;    // ignore deflections < 5% of radius (prevents drift)
        self._KNOB_DOT_RADIUS = 12;    // px — size of the draggable indicator dot
        self._KNOB_PADDING = 10;       // px — gap between canvas edge and outer circle

        self._drawKnob = function (dx, dy) {
            var canvas = document.getElementById("octocarvera-xy-knob");
            if (!canvas) return;
            var ctx = canvas.getContext("2d");
            var cx = canvas.width / 2, cy = canvas.height / 2, r = canvas.width / 2 - self._KNOB_PADDING;
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            // Outer circle
            ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2);
            ctx.strokeStyle = "#ccc"; ctx.lineWidth = 2; ctx.stroke();
            // Crosshairs
            ctx.beginPath();
            ctx.moveTo(cx, cy - r); ctx.lineTo(cx, cy + r);
            ctx.moveTo(cx - r, cy); ctx.lineTo(cx + r, cy);
            ctx.strokeStyle = "#eee"; ctx.lineWidth = 1; ctx.stroke();
            // Dot (indicator follows mouse/touch position)
            ctx.beginPath(); ctx.arc(cx + dx, cy + dy, self._KNOB_DOT_RADIUS, 0, Math.PI * 2);
            ctx.fillStyle = self._knobDragging ? "#337ab7" : "#666"; ctx.fill();
        };

        // Convert a mouse/touch event to knob-relative (dx, dy) clamped to the circle.
        self._getKnobOffset = function (e) {
            var canvas = document.getElementById("octocarvera-xy-knob");
            if (!canvas) return {x: 0, y: 0};
            var rect = canvas.getBoundingClientRect();
            var cx = canvas.width / 2, cy = canvas.height / 2;
            var clientX = e.clientX !== undefined ? e.clientX : (e.touches && e.touches[0] ? e.touches[0].clientX : cx + rect.left);
            var clientY = e.clientY !== undefined ? e.clientY : (e.touches && e.touches[0] ? e.touches[0].clientY : cy + rect.top);
            var x = clientX - rect.left - cx;
            var y = clientY - rect.top - cy;
            var r = canvas.width / 2 - self._KNOB_PADDING;
            var dist = Math.sqrt(x * x + y * y);
            if (dist > r) { x = x / dist * r; y = y / dist * r; }
            return {x: x, y: y, r: r};
        };

        self._knobCurrentOff = {x: 0, y: 0, r: 90};

        self._knobSendContinuous = function () {
            if (!self._knobDragging || self.motionBlocked()) return;
            var off = self._knobCurrentOff;
            var r = off.r || 90;
            var normX = off.x / r; // -1 to 1
            var normY = -off.y / r; // -1 to 1, Y inverted

            // Magnitude (0 to 1) with deadzone
            var mag = Math.sqrt(normX * normX + normY * normY);
            if (mag < self._KNOB_DEADZONE) return;

            // Squared for fine control near center
            var speed = mag * mag * self._knobMaxFeed; // mm/min
            // Distance = speed * interval (in minutes)
            var intervalMin = self._knobSendInterval / 60000;
            var totalDist = speed * intervalMin;

            // Split into X/Y components based on direction
            var dirX = normX / mag;
            var dirY = normY / mag;
            var distX = dirX * totalDist;
            var distY = dirY * totalDist;

            if (Math.abs(distX) > 0.001 || Math.abs(distY) > 0.001) {
                OctoPrint.simpleApiCommand("octocarvera", "jog", {x: distX, y: distY, z: 0, feed: Math.round(speed)});
            }
        };

        self._knobStartContinuous = function () {
            if (self._knobTimer) return;
            self._knobTimer = setInterval(function () {
                self._knobSendContinuous();
            }, self._knobSendInterval);
        };

        self._knobStopContinuous = function () {
            if (self._knobTimer) {
                clearInterval(self._knobTimer);
                self._knobTimer = null;
            }
        };

        self.knobMouseDown = function (vm, e) {
            if (self.motionBlocked()) return true;
            self._knobDragging = true;
            self._knobCurrentOff = self._getKnobOffset(e);
            self._drawKnob(self._knobCurrentOff.x, self._knobCurrentOff.y);
            self._knobStartContinuous();
            return true;
        };
        self.knobMouseMove = function (vm, e) {
            if (!self._knobDragging) return true;
            self._knobCurrentOff = self._getKnobOffset(e);
            self._drawKnob(self._knobCurrentOff.x, self._knobCurrentOff.y);
            return true;
        };
        self.knobMouseUp = function (vm, e) {
            self._knobDragging = false;
            self._knobStopContinuous();
            self._drawKnob(0, 0);
            return true;
        };
        self.knobTouchStart = function (vm, e) {
            if (self.motionBlocked()) return true;
            self._knobDragging = true;
            self._knobCurrentOff = self._getKnobOffset(e);
            self._drawKnob(self._knobCurrentOff.x, self._knobCurrentOff.y);
            self._knobStartContinuous();
            return true;
        };
        self.knobTouchMove = function (vm, e) {
            if (!self._knobDragging) return true;
            self._knobCurrentOff = self._getKnobOffset(e);
            self._drawKnob(self._knobCurrentOff.x, self._knobCurrentOff.y);
            e.preventDefault();
            return false;
        };
        self.knobTouchEnd = function (vm, e) {
            self._knobDragging = false;
            self._knobStopContinuous();
            self._drawKnob(0, 0);
            return true;
        };

        // === SD Card File Operations ===

        self.refreshFiles = function () {
            self.sdLoading(true);
            self.sdError(null);
            OctoPrint.simpleApiCommand("octocarvera", "list_files", {path: self.sdPath()})
                .done(function (data) {
                    if (data.ok) {
                        self.sdFiles(data.files);
                    } else {
                        self.sdError(data.error || "Failed to list files");
                    }
                })
                .fail(function (xhr) {
                    var msg = "Failed to list files";
                    try { msg = JSON.parse(xhr.responseText).error || msg; } catch (e) {}
                    self.sdError(msg);
                })
                .always(function () {
                    self.sdLoading(false);
                });
        };

        self.navigateToDir = function (name) {
            var path = self.sdPath().replace(/\/$/, "");
            self.sdPath(path + "/" + name);
            self.refreshFiles();
        };

        self.navigateUp = function () {
            var path = self.sdPath().replace(/\/$/, "");
            if (path === "/sd") return;
            var parent = path.substring(0, path.lastIndexOf("/"));
            if (!parent || parent.length < 3) parent = "/sd";
            self.sdPath(parent);
            self.refreshFiles();
        };

        self.formatSize = function (bytes) {
            if (bytes < 1024) return bytes + " B";
            if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
            return (bytes / (1024 * 1024)).toFixed(1) + " MB";
        };

        // Convert a "YYYY-MM-DD HH:MM" timestamp to a relative "X ago" string.
        // Falls back to the raw date string for anything older than 30 days.
        self.formatTimeAgo = function (dateStr) {
            if (!dateStr) return "?";
            var parts = dateStr.split(/[- :]/);
            if (parts.length < 5) return dateStr;
            var d = new Date(parts[0], parts[1] - 1, parts[2], parts[3], parts[4]);
            var now = new Date();
            var diff = Math.floor((now - d) / 1000);
            if (diff < 60) return "just now";
            if (diff < 3600) return Math.floor(diff / 60) + " min ago";
            if (diff < 86400) return Math.floor(diff / 3600) + " hours ago";       // 24h
            if (diff < 2592000) return Math.floor(diff / 86400) + " days ago";      // 30d
            return dateStr;
        };

        self.sdClearSearch = function () {
            self.sdSearchQuery("");
        };

        self.sdChangeSorting = function (field) {
            if (self.sdSortBy() === field) {
                self.sdSortAsc(!self.sdSortAsc());
            } else {
                self.sdSortBy(field);
                self.sdSortAsc(field === "name");
            }
        };

        self.sdChangeListStyle = function (style) {
            self.sdListStyle(style);
        };

        self.sdDeleteFile = function (entry) {
            var path = self.sdPath().replace(/\/$/, "") + "/" + entry.name;
            var msg = entry.is_dir ? "Delete folder '" + entry.name + "' and all its contents?" : "Delete '" + entry.name + "'?";
            if (!confirm(msg)) return;
            OctoPrint.simpleApiCommand("octocarvera", "delete_file", {path: path})
                .done(function (data) {
                    if (data.ok) {
                        self.refreshFiles();
                    } else {
                        self.sdError(data.error || "Delete failed");
                    }
                })
                .fail(function (xhr) {
                    var msg = "Delete failed";
                    try { msg = JSON.parse(xhr.responseText).error || msg; } catch (e) {}
                    self.sdError(msg);
                });
        };

        self.sdMoveFile = function (entry) {
            if (entry.is_dir) return;  // files only, per design
            var src = self.sdPath().replace(/\/$/, "") + "/" + entry.name;
            var dst = prompt("Rename / move to:", src);
            if (!dst) return;
            dst = dst.trim();
            if (!dst || dst === src) return;
            OctoPrint.simpleApiCommand("octocarvera", "move_file", {src: src, dst: dst})
                .done(function (data) {
                    if (data.ok) {
                        self.refreshFiles();
                    } else {
                        self.sdError(data.error || "Move failed");
                    }
                })
                .fail(function (xhr) {
                    var msg = "Move failed";
                    try { msg = JSON.parse(xhr.responseText).error || msg; } catch (e) {}
                    self.sdError(msg);
                });
        };

        self.sdCreateFolder = function () {
            var name = prompt("New folder name:");
            if (!name || !name.trim()) return;
            name = name.trim();
            var path = self.sdPath().replace(/\/$/, "") + "/" + name;
            OctoPrint.simpleApiCommand("octocarvera", "create_folder", {path: path})
                .done(function (data) {
                    if (data.ok) {
                        self.refreshFiles();
                    } else {
                        self.sdError(data.error || "Create folder failed");
                    }
                })
                .fail(function (xhr) {
                    var msg = "Create folder failed";
                    try { msg = JSON.parse(xhr.responseText).error || msg; } catch (e) {}
                    self.sdError(msg);
                });
        };

        self.cancelUpload = function () {
            OctoPrint.simpleApiCommand("octocarvera", "cancel_upload");
        };

        // Start an XMODEM upload to the Carvera's SD card. Warns the user
        // if the file is large (>2 min estimated transfer time at ~1.3 KB/s).
        self.uploadToCarvera = function () {
            var filename = self.selectedUploadFile();
            if (!filename) return;

            // Check file size and warn if transfer will be slow
            var fileInfo = self.octoprintFiles().find(function (f) { return f.name === filename; });
            if (fileInfo && fileInfo.size) {
                var estimatedSecs = fileInfo.size / self._XMODEM_RATE;
                if (estimatedSecs > 120) {
                    var mins = Math.ceil(estimatedSecs / 60);
                    var sizeKB = (fileInfo.size / 1024).toFixed(0);
                    if (!confirm("This file (" + sizeKB + " KB) will take approximately " + mins + " minutes to transfer over serial. Continue?")) {
                        return;
                    }
                }
            }

            var remotePath = self.sdPath().replace(/\/$/, "") + "/" + filename;
            self.uploadInProgress(true);
            self.uploadFilename(filename);
            self.uploadPercent(0);
            self.uploadEta(null);
            self.uploadSpeed(null);
            OctoPrint.simpleApiCommand("octocarvera", "upload_to_carvera", {
                filename: filename,
                remote_path: remotePath
            }).fail(function (xhr) {
                self.uploadInProgress(false);
                var msg = "Upload failed";
                try { msg = JSON.parse(xhr.responseText).error || msg; } catch (e) {}
                self.sdError(msg);
            });
        };

        // === Firmware flash (in the Settings dialog, plain JS DOM) ===
        //
        // The Flash Firmware UI lives in octocarvera_settings.jinja2 and is
        // NOT bound to this view model by Knockout (OctoPrint's settings
        // dialog binds settingsViewModel, not us, to plugin settings panels).
        // So everything below pokes the DOM directly from the lifecycle
        // hooks. Small, short-lived form: no reactive state needed.

        self._flashStatus = function (text, cls) {
            var el = document.getElementById("octocarvera-flash-status");
            if (!el) return;
            if (!text) {
                el.style.display = "none";
                el.textContent = "";
                return;
            }
            el.className = "alert " + (cls || "");
            el.textContent = text;
            el.style.display = "";
        };

        self._refreshFlashSettingsUI = function () {
            var binaryDiv = document.getElementById("octocarvera-flash-binary");
            var normalDiv = document.getElementById("octocarvera-flash-normal");
            if (!binaryDiv || !normalDiv) return;  // settings panel not rendered yet

            var mode = null;
            try {
                mode = self.settingsViewModel.settings.plugins.octocarvera.protocol_mode();
            } catch (e) {}
            var version = self.firmwareVersion() || "unknown";

            var fwBinary = document.getElementById("octocarvera-fw-version-binary");
            var fwNormal = document.getElementById("octocarvera-fw-version-normal");
            if (fwBinary) fwBinary.textContent = version;
            if (fwNormal) fwNormal.textContent = version;

            if (mode === "binary") {
                binaryDiv.style.display = "";
                normalDiv.style.display = "none";
            } else {
                binaryDiv.style.display = "none";
                normalDiv.style.display = "";
                self._populateFlashDropdown();
            }
            self._flashStatus(null);
        };

        self._populateFlashDropdown = function () {
            var select = document.getElementById("octocarvera-flash-select");
            if (!select) return;
            var currentValue = select.value;
            // Remove all options except the placeholder
            while (select.options.length > 1) select.remove(1);
            self.octoprintFiles().forEach(function (f) {
                if (/\.bin$/i.test(f.name)) {
                    var opt = document.createElement("option");
                    opt.value = f.name;
                    opt.textContent = f.name;
                    select.appendChild(opt);
                }
            });
            // Restore previous selection if it still exists
            if (currentValue) {
                for (var i = 0; i < select.options.length; i++) {
                    if (select.options[i].value === currentValue) {
                        select.selectedIndex = i;
                        break;
                    }
                }
            }
            self._updateFlashButtonEnabled();
        };

        self._updateFlashButtonEnabled = function () {
            var btn = document.getElementById("octocarvera-flash-btn");
            var sel = document.getElementById("octocarvera-flash-select");
            if (!btn || !sel) return;
            btn.disabled = !sel.value;
        };

        self._doFlashFirmware = function () {
            var sel = document.getElementById("octocarvera-flash-select");
            if (!sel || !sel.value) return;
            var filename = sel.value;
            if (!/\.bin$/i.test(filename)) {
                self._flashStatus("Firmware files must end in .bin", "alert-error");
                return;
            }
            // Find file size for the confirmation dialog
            var fileInfo = self.octoprintFiles().find(function (f) { return f.name === filename; });
            var sizeKB = fileInfo ? Math.round(fileInfo.size / 1024) : "?";

            // Double confirmation — two explicit "are you sure" gates
            if (!confirm(
                "You are about to upload " + filename + " (" + sizeKB + " KB) " +
                "to the Carvera's SD card as firmware.bin.\n\n" +
                "After upload you'll choose whether to reboot (flash) or cancel.\n\n" +
                "Continue with the upload?"
            )) return;

            if (!confirm(
                "WARNING: Flashing incorrect or corrupt firmware can permanently " +
                "brick the Carvera (continuous beeping, won't boot).\n\n" +
                "Are you ABSOLUTELY SURE this is a valid Carvera firmware binary?"
            )) return;

            self._flashStatus("Uploading " + filename + " to SD card as firmware.tmp (staging)...", "alert-info");
            self._showFlashProgress(true);
            var btn = document.getElementById("octocarvera-flash-btn");
            if (btn) btn.disabled = true;
            OctoPrint.simpleApiCommand("octocarvera", "flash_firmware", {
                filename: filename,
            })
                .done(function (data) {
                    // Progress and completion messages flow through
                    // onDataUpdaterPluginMessage → _flashStatus.
                })
                .fail(function (xhr) {
                    self._showFlashProgress(false);
                    var body = null;
                    try { body = JSON.parse(xhr.responseText); } catch (e) {}
                    var msg = (body && (body.message || body.error)) || "Flash failed";
                    self._flashStatus(msg, "alert-error");
                    self._updateFlashButtonEnabled();
                });
        };

        self._showFlashProgress = function (show) {
            var el = document.getElementById("octocarvera-flash-progress");
            if (el) el.style.display = show ? "" : "none";
        };

        self._showFlashChoicePanel = function (show) {
            var el = document.getElementById("octocarvera-flash-choice");
            if (el) el.style.display = show ? "" : "none";
        };

        self._flashFirmwareAction = function (action) {
            self._showFlashChoicePanel(false);
            OctoPrint.simpleApiCommand("octocarvera", "flash_firmware_action", {action: action})
                .done(function () {
                    if (action === "reboot") {
                        self._flashStatus("Machine is rebooting. The bootloader will flash the new firmware on boot.", "alert-warning");
                    } else if (action === "keep") {
                        self._flashStatus("firmware.bin is waiting on the SD card. It will flash on the next power cycle. You can still delete it.", "alert-info");
                    } else if (action === "delete") {
                        self._flashStatus("Firmware file deleted from SD card. Machine unchanged.", "alert-success");
                    }
                    self._updateFlashButtonEnabled();
                })
                .fail(function () {
                    self._flashStatus("Failed to perform action — check the machine manually.", "alert-error");
                    self._updateFlashButtonEnabled();
                });
        };

        self._initFlashSettingsListeners = function () {
            var btn = document.getElementById("octocarvera-flash-btn");
            var refresh = document.getElementById("octocarvera-flash-refresh");
            var sel = document.getElementById("octocarvera-flash-select");
            if (btn && !btn._octocarveraBound) {
                btn.addEventListener("click", self._doFlashFirmware);
                btn._octocarveraBound = true;
            }
            if (refresh && !refresh._octocarveraBound) {
                refresh.addEventListener("click", function () {
                    self._loadOctoPrintFiles();
                    setTimeout(self._refreshFlashSettingsUI, 200);
                });
                refresh._octocarveraBound = true;
            }
            if (sel && !sel._octocarveraBound) {
                sel.addEventListener("change", self._updateFlashButtonEnabled);
                sel._octocarveraBound = true;
            }
            // Post-upload choice buttons
            ["reboot", "keep", "delete"].forEach(function (action) {
                var el = document.getElementById("octocarvera-flash-" + action);
                if (el && !el._octocarveraBound) {
                    el.addEventListener("click", function () { self._flashFirmwareAction(action); });
                    el._octocarveraBound = true;
                }
            });
        };

        self.onSettingsShown = function () {
            self._initFlashSettingsListeners();
            self._refreshFlashSettingsUI();
        };

        // Fetch OctoPrint's local file list (used by the upload selector and
        // firmware flash dropdown). Tries the JS client API first, falls back
        // to a direct REST call if the client helper isn't available.
        self._loadOctoPrintFiles = function () {
            OctoPrint.files.listForLocation("local")
                .done(function (data) {
                    var files = [];
                    var fileList = data && data.files ? data.files : (Array.isArray(data) ? data : []);
                    for (var i = 0; i < fileList.length; i++) {
                        var f = fileList[i];
                        if (f.type !== "folder") {
                            files.push({name: f.name, size: f.size});
                        }
                    }
                    self.octoprintFiles(files);
                })
                .fail(function () {
                    // Fallback: fetch via REST API directly
                    $.getJSON(OctoPrint.getBaseUrl() + "api/files/local", {apikey: OctoPrint.options.apikey})
                        .done(function (data) {
                            var files = [];
                            if (data && data.files) {
                                for (var i = 0; i < data.files.length; i++) {
                                    var f = data.files[i];
                                    if (f.type !== "folder") {
                                        files.push({name: f.name, size: f.size});
                                    }
                                }
                            }
                            self.octoprintFiles(files);
                        });
                });
        };

        // Bind the select element to selectedUploadFile
        self._bindUploadSelect = function () {
            var select = document.getElementById("octocarvera-upload-select");
            if (select) {
                select.addEventListener("change", function () {
                    self.selectedUploadFile(this.value || null);
                });
            }
        };

        // === Status Updates ===
        // Apply a status payload from the backend to all observables. Called
        // on each ~0.3s status poll and on initial GET after startup.
        self._updateStatus = function (data) {
            self.state(data.state);
            if (data.activity) self.activity(data.activity);
            if (data.allowed_actions) self.allowedActions(data.allowed_actions);
            if (data.machine_pos) {
                self.machineX(data.machine_pos.x.toFixed(3));
                self.machineY(data.machine_pos.y.toFixed(3));
                self.machineZ(data.machine_pos.z.toFixed(3));
                self.machineA(data.machine_pos.a.toFixed(3));
                self.machineB(data.machine_pos.b.toFixed(3));
            }
            if (data.work_pos) {
                var newX = data.work_pos.x.toFixed(3);
                var newY = data.work_pos.y.toFixed(3);
                var newZ = data.work_pos.z.toFixed(3);
                self.workX(newX); self.workY(newY); self.workZ(newZ);
                self.workA(data.work_pos.a.toFixed(3));
                self.workB(data.work_pos.b.toFixed(3));
                // Only update goto fields if user hasn't edited them
                // Compare current field value to last machine-set value
                var newA = data.work_pos.a.toFixed(3);
                if (self.gotoX() === self._lastWPosX) { self.gotoX(newX); self._lastWPosX = newX; }
                if (self.gotoY() === self._lastWPosY) { self.gotoY(newY); self._lastWPosY = newY; }
                if (self.gotoZ() === self._lastWPosZ) { self.gotoZ(newZ); self._lastWPosZ = newZ; }
                if (self.gotoA() === self._lastWPosA) { self.gotoA(newA); self._lastWPosA = newA; }
            }
            if (data.feed) {
                self.feedCurrent(Math.round(data.feed.current));
                self.feedMax(Math.round(data.feed.max));
                self.feedOverrideDisplay(Math.round(data.feed.override));
                if (!self._feedSliderActive) self.feedOverride(Math.round(data.feed.override));
            }
            if (data.spindle) {
                self.spindleCurrent(Math.round(data.spindle.current));
                self.spindleMax(Math.round(data.spindle.max));
                self.spindleTemp(data.spindle.spindle_temp.toFixed(1) + " / " + data.spindle.power_temp.toFixed(1));
                self.spindleOverrideDisplay(Math.round(data.spindle.override));
                if (!self._spindleSliderActive) self.spindleOverride(Math.round(data.spindle.override));
            }
            if (data.tool) self.toolNumber(data.tool.number);
            if (data.wpvoltage !== undefined && data.wpvoltage !== null) self.wpVoltage(typeof data.wpvoltage === 'number' ? data.wpvoltage.toFixed(2) : data.wpvoltage);
            if (data.laser) {
                self.laserMode(data.laser.mode);
                self.laserPower(data.laser.power.toFixed(1));
                self.laserScale(data.laser.scale.toFixed(1));
            }
            if (data.config) {
                // Carvera firmware model IDs (from config.model in status response)
                var models = {1: "Carvera", 2: "Carvera Air"};
                self.machineModel(models[data.config.model] || "Unknown (" + data.config.model + ")");
            }
            if (data.halt_reason !== undefined && data.halt_reason !== null) self.haltReason(data.halt_reason);
            if (data.playback) {
                self.playbackPercent(data.playback.percent);
                var secs = data.playback.elapsed_secs;
                var m = Math.floor(secs / 60), s = secs % 60;
                self.playbackElapsed(m + ":" + (s < 10 ? "0" : "") + s);
            } else {
                self.playbackPercent(null);
                self.playbackElapsed(null);
            }
            if (data.firmware_version) self.firmwareVersion(data.firmware_version);
            if (data.plugin_version) self.pluginVersion(data.plugin_version);
        };

        // Handle messages from the backend plugin. Message types:
        //   status         — periodic machine state update (~0.3s)
        //   connected      — serial connection established
        //   disconnected   — serial connection lost
        //   upload_progress — XMODEM transfer progress (percent, ETA, speed)
        //   upload_complete — transfer finished successfully
        //   upload_error    — transfer failed or was cancelled
        //   firmware_staged — firmware.bin ready on SD, show choice panel
        //   firmware_flash_cleanup — partial firmware file removed
        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin !== "octocarvera") return;
            if (data.type === "status") {
                self._updateStatus(data);
            } else if (data.type === "connected") {
                self.connected(true);
                self.state(data.state);
                if (!self.uploadInProgress()) {
                    self.refreshFiles();
                }
            } else if (data.type === "disconnected") {
                self.connected(false);
                self.state("Unknown");
                self.activity("unknown");
                self.allowedActions([]);
            } else if (data.type === "upload_progress") {
                self.uploadInProgress(true);
                self.uploadFilename(data.filename);
                self.uploadPercent(data.percent);
                // Mirror progress to the settings flash progress bar
                var flashBar = document.getElementById("octocarvera-flash-bar");
                var flashPct = document.getElementById("octocarvera-flash-pct");
                if (flashBar) flashBar.style.width = data.percent + "%";
                if (flashPct) flashPct.textContent = data.percent + "%";
                // Compute ETA and speed from elapsed time
                if (data.elapsed_secs && data.percent > 0 && data.percent < 100) {
                    var elapsed = data.elapsed_secs;
                    var estimated_total = elapsed / (data.percent / 100);
                    var remaining = Math.max(0, Math.round(estimated_total - elapsed));
                    var mins = Math.floor(remaining / 60);
                    var secs = remaining % 60;
                    self.uploadEta("~" + mins + "m " + (secs < 10 ? "0" : "") + secs + "s remaining");
                    if (data.bytes_sent && elapsed > 0) {
                        var kbps = (data.bytes_sent / 1024 / elapsed).toFixed(1);
                        self.uploadSpeed(kbps + " KB/s");
                    }
                } else {
                    self.uploadEta(null);
                    self.uploadSpeed(null);
                }
            } else if (data.type === "upload_complete") {
                self.uploadInProgress(false);
                self.uploadPercent(100);
                self.uploadEta(null);
                self.uploadSpeed(null);
                self.refreshFiles(); // Reload to show new file
                // If the Settings Flash UI is open, advance its status line
                // (the DOM helper is a no-op when the element isn't rendered).
                self._showFlashProgress(false);
            } else if (data.type === "firmware_staged") {
                self._showFlashProgress(false);
                self._flashStatus("Firmware uploaded and staged as /sd/firmware.bin. Choose what to do:", "alert-warning");
                self._showFlashChoicePanel(true);
            } else if (data.type === "firmware_flash_cleanup") {
                self._showFlashProgress(false);
                self._flashStatus("Partial firmware file cleaned up. Machine unchanged.", "alert-info");
                self._updateFlashButtonEnabled();
            } else if (data.type === "upload_error") {
                self.uploadInProgress(false);
                self.uploadEta(null);
                self.uploadSpeed(null);
                self.sdError("Upload failed: " + (data.error || "unknown error"));
                self._flashStatus("Flash failed: " + (data.error || "unknown error"), "alert-error");
                self._updateFlashButtonEnabled();
            }
        };

        // === Slider binding helper ===
        // Bridges an HTML range input with a Knockout observable and an API
        // call. The activeFlag prevents incoming status updates from moving
        // the slider while the user is dragging it (which would fight their
        // input). On release ("change"), the new value is sent to the backend.
        self._bindSlider = function (id, observable, activeFlag, sendFn) {
            var slider = document.getElementById(id);
            if (!slider) return;
            slider.value = observable();
            slider.addEventListener("mousedown", function () { self[activeFlag] = true; });
            slider.addEventListener("touchstart", function () { self[activeFlag] = true; });
            slider.addEventListener("input", function () { observable(parseInt(this.value)); });
            slider.addEventListener("change", function () { self[activeFlag] = false; sendFn(); });
            slider.addEventListener("mouseup", function () { self[activeFlag] = false; });
            slider.addEventListener("touchend", function () { self[activeFlag] = false; });
            observable.subscribe(function (val) { if (!self[activeFlag]) slider.value = val; });
        };

        // OctoPrint lifecycle: called once when all view models are ready.
        // Initializes the UI, fetches initial state, and rearranges sidebar.
        self.onStartupComplete = function () {
            OctoPrint.simpleApiGet("octocarvera").done(function (data) {
                if (data.connected) {
                    self.connected(true);
                    self._updateStatus(data);
                }
            });
            // Draw initial knob
            setTimeout(function () { self._drawKnob(0, 0); }, 500);
            // Bind override sliders (must wait for tab to render)
            self._initSliders();
            // Load OctoPrint files for upload selector
            self._loadOctoPrintFiles();
            // Auto-refresh Carvera SD file list
            self.refreshFiles();
            // Bind upload select
            setTimeout(function () { self._bindUploadSelect(); }, 1000);
            // Rename OctoPrint's "Files" sidebar to "OctoPrint Files"
            $("#files_wrapper .accordion-heading a, #files .accordion-heading a").each(function () {
                if ($(this).text().trim() === "Files") {
                    $(this).text("OctoPrint Files");
                }
            });
            // Hide OctoPrint's native "State" sidebar panel
            $("#state_wrapper, #state").closest(".accordion-group").hide();
            // Reorder sidebar: Carvera Files → Transfer Files → OctoPrint Files
            var carveraFiles = $("#sidebar_plugin_octocarvera_files").closest(".accordion-group");
            var transferFiles = $("#sidebar_plugin_octocarvera_transfer").closest(".accordion-group");
            var octoprintFiles = $("#files_wrapper").length ? $("#files_wrapper") : $("#files").closest(".accordion-group");
            if (carveraFiles.length && transferFiles.length) {
                transferFiles.insertAfter(carveraFiles);
            }
            if (transferFiles.length && octoprintFiles.length) {
                octoprintFiles.insertAfter(transferFiles);
            }
        };

        self._initSliders = function () {
            // Try immediately, retry if tab not rendered yet
            var attempts = 0;
            var tryBind = function () {
                var feedSlider = document.getElementById("octocarvera-feed-slider");
                if (feedSlider) {
                    self._bindSlider("octocarvera-feed-slider", self.feedOverride, "_feedSliderActive", self.setFeedOverride);
                    self._bindSlider("octocarvera-spindle-slider", self.spindleOverride, "_spindleSliderActive", self.setSpindleOverride);
                } else if (attempts < 10) {
                    attempts++;
                    setTimeout(tryBind, 500);
                }
            };
            setTimeout(tryBind, 500);
        };

        // Re-bind sliders when tab becomes visible
        self.onTabChange = function (current, previous) {
            if (current === "#tab_plugin_octocarvera") {
                setTimeout(function () { self._initSliders(); self._drawKnob(0, 0); }, 200);
            }
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: OctoCarveraViewModel,
        dependencies: ["settingsViewModel"],
        elements: ["#sidebar_plugin_octocarvera", "#sidebar_plugin_octocarvera_machine_status", "#tab_plugin_octocarvera", "#sidebar_plugin_octocarvera_files", "#sidebar_plugin_octocarvera_transfer"],
    });
});
