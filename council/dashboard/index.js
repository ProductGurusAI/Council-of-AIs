document.addEventListener("DOMContentLoaded", () => {
    // Safety aliases for external CDN dependencies
    const json = JSON;
    const lucide = window.lucide || { createIcons: () => {} };

    // Initialize Lucide Icons
    lucide.createIcons();

    // State Variables
    let activePanel = "panel-overview";
    let activeTaskId = null;
    let selectedTaskId = null;
    let graphData = { nodes: [], edges: [] };
    let forceSimulationActive = false;

    // Dom Elements
    const navButtons = document.querySelectorAll(".nav-btn");
    const panels = document.querySelectorAll(".content-panel");
    const pageTitle = document.getElementById("page-title");
    
    // Overview Elements
    const lblBudgetSpent = document.getElementById("lbl-budget-spent");
    const barBudgetProgress = document.getElementById("bar-budget-progress");
    const statRemainingBudget = document.getElementById("stat-remaining-budget");
    const statTotalSpent = document.getElementById("stat-total-spent");
    const statOverheadPct = document.getElementById("stat-overhead-pct");
    const statOverheadSpent = document.getElementById("stat-overhead-spent");
    const statEscalationCount = document.getElementById("stat-escalation-count");
    const costThinker = document.getElementById("cost-thinker");
    const costExplorer = document.getElementById("cost-explorer");
    const barThinker = document.getElementById("bar-thinker");
    const barExplorer = document.getElementById("bar-explorer");
    const ctrlTaskCap = document.getElementById("ctrl-task-cap");
    const ctrlReserveFloor = document.getElementById("ctrl-reserve-floor");
    const ctrlOverrideCount = document.getElementById("ctrl-override-count");

    // Task Explorer Elements
    const taskSearchInput = document.getElementById("task-search-input");
    const taskListContainer = document.getElementById("task-list-container");
    const taskDetailsPane = document.getElementById("task-details-pane");

    // Graph Canvas Elements
    const graphCanvas = document.getElementById("graph-canvas");
    const graphCtx = graphCanvas.getContext("2d");
    const btnReindexGraph = document.getElementById("btn-reindex-graph");

    // Playground Chat Elements
    const playgroundChatHistory = document.getElementById("playground-chat-history");
    const txtChatInput = document.getElementById("txt-chat-input");
    const btnChatSend = document.getElementById("btn-chat-send");
    const btnChatClose = document.getElementById("btn-chat-close");
    const playgroundCloseBar = document.getElementById("playground-close-bar");
    const lblActiveTaskId = document.getElementById("lbl-active-task-id");

    // Endgame Modal Elements
    const endgameModal = document.getElementById("endgame-modal");
    const endgameModalMsg = document.getElementById("endgame-modal-msg");
    const btnModalConfirm = document.getElementById("btn-modal-confirm");
    const btnModalCancel = document.getElementById("btn-modal-cancel");

    // Mobile Navigation Controls
    const btnMobileToggle = document.getElementById("btn-mobile-toggle");
    const sidebarMenu = document.getElementById("sidebar-menu");

    if (btnMobileToggle && sidebarMenu) {
        btnMobileToggle.addEventListener("click", (e) => {
            e.stopPropagation();
            const isOpen = sidebarMenu.classList.toggle("open");
            btnMobileToggle.setAttribute("aria-expanded", isOpen);
        });

        // Close sidebar if clicked outside of it on mobile
        document.addEventListener("click", (e) => {
            if (sidebarMenu.classList.contains("open") && !sidebarMenu.contains(e.target) && e.target !== btnMobileToggle) {
                sidebarMenu.classList.remove("open");
                btnMobileToggle.setAttribute("aria-expanded", "false");
            }
        });
    }

    // ========================================================
    // Navigation / Routing
    // ========================================================
    navButtons.forEach(btn => {
        btn.addEventListener("click", () => {
            selectPanel(btn);
            if (sidebarMenu) {
                sidebarMenu.classList.remove("open");
                btnMobileToggle.setAttribute("aria-expanded", "false");
            }
        });
    });

    function selectPanel(selectedBtn) {
        navButtons.forEach(b => {
            b.classList.remove("active");
            b.setAttribute("aria-selected", "false");
            b.setAttribute("tabindex", "-1");
        });
        selectedBtn.classList.add("active");
        selectedBtn.setAttribute("aria-selected", "true");
        selectedBtn.setAttribute("tabindex", "0");

        const target = selectedBtn.getAttribute("data-target");
        activePanel = target;

        panels.forEach(p => p.classList.remove("active"));
        document.getElementById(target).classList.add("active");

        // Update title
        const panelTitles = {
            "panel-overview": "Overview Dashboard",
            "panel-tasks": "Task Explorer",
            "panel-graph": "Codebase Graph Touch-List",
            "panel-playground": "Interactive Chat Playground",
            "panel-chamber": "Council Chamber"
        };
        pageTitle.innerText = panelTitles[target] || "Dashboard";

        // Trigger reload data on route change
        if (target === "panel-overview") {
            fetchStats();
        } else if (target === "panel-tasks") {
            fetchTasks();
        } else if (target === "panel-graph") {
            fetchGraph();
        }
    }

    // Keyboard Tab list Navigation
    const tabList = document.querySelector('.nav-menu');
    if (tabList) {
        tabList.addEventListener('keydown', (e) => {
            const tabs = Array.from(navButtons);
            const index = tabs.indexOf(document.activeElement);
            if (index === -1) return;

            let nextIndex = index;
            if (e.key === 'ArrowDown' || e.key === 'ArrowRight') {
                nextIndex = (index + 1) % tabs.length;
                e.preventDefault();
            } else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') {
                nextIndex = (index - 1 + tabs.length) % tabs.length;
                e.preventDefault();
            } else if (e.key === 'Home') {
                nextIndex = 0;
                e.preventDefault();
            } else if (e.key === 'End') {
                nextIndex = tabs.length - 1;
                e.preventDefault();
            }

            if (nextIndex !== index) {
                tabs[nextIndex].focus();
            }
        });
    }

    // ========================================================
    // API Fetch Overview Stats
    // ========================================================
    function fetchStats() {
        // Toggle skeletons on cards during fetch
        const valContainers = [statRemainingBudget, statTotalSpent, statOverheadPct, statEscalationCount];
        valContainers.forEach(el => el.classList.add("skeleton-pulse"));

        fetch("/api/stats")
            .then(res => res.json())
            .then(data => {
                valContainers.forEach(el => el.classList.remove("skeleton-pulse"));
                const total = data.total_budget || 75.00;
                const spent = data.total_spent || 0.00;
                const remaining = data.remaining_budget || 0.00;
                const pct = Math.min(100, (spent / total) * 100);

                // Update Header Budget Progress
                lblBudgetSpent.innerText = `$${spent.toFixed(2)} / $${total.toFixed(2)}`;
                barBudgetProgress.style.width = `${pct}%`;

                // Update Cards
                statRemainingBudget.innerText = `$${remaining.toFixed(2)}`;
                statTotalSpent.innerText = `$${spent.toFixed(2)}`;
                statOverheadPct.innerText = `${(data.orchestration_overhead_pct || 0.0).toFixed(2)}%`;
                statOverheadSpent.innerText = `Spent: $${(data.orchestration_spent || 0.0).toFixed(4)}`;
                statEscalationCount.innerText = data.failed_then_escalated_count || 0;

                // Update Controls
                ctrlTaskCap.innerText = `$${(data.per_task_cap || 3.00).toFixed(2)}`;
                ctrlReserveFloor.innerText = `$${(data.reserve_floor || 10.00).toFixed(2)}`;
                ctrlOverrideCount.innerText = data.override_count || 0;

                const checkboxParallel = document.getElementById("chamber-parallel");
                if (checkboxParallel) {
                    if (data.sandbox_present === false || data.reservations_present === false) {
                        checkboxParallel.disabled = true;
                        checkboxParallel.parentElement.style.opacity = "0.5";
                        checkboxParallel.parentElement.title = "Requires container sandbox and budget reservations enabled.";
                    } else {
                        checkboxParallel.disabled = false;
                        checkboxParallel.parentElement.style.opacity = "1";
                        checkboxParallel.parentElement.title = "";
                    }
                }

                // Update Average Route costs
                const thinkerCost = data.avg_spend_per_turn_by_route?.thinker || 0.0;
                const explorerCost = data.avg_spend_per_turn_by_route?.explorer || 0.0;

                costThinker.innerText = `$${thinkerCost.toFixed(5)}`;
                costExplorer.innerText = `$${explorerCost.toFixed(5)}`;

                // Max cost scaling for progress bar
                const maxCost = Math.max(0.001, thinkerCost, explorerCost);
                barThinker.style.width = `${(thinkerCost / maxCost) * 100}%`;
                barExplorer.style.width = `${(explorerCost / maxCost) * 100}%`;
            })
            .catch(err => {
                valContainers.forEach(el => el.classList.remove("skeleton-pulse"));
                console.error("Error fetching stats:", err);
            });
    }

    // ========================================================
    // API Fetch Tasks
    // ========================================================
    let allTasks = [];
    function fetchTasks() {
        taskListContainer.innerHTML = `
            <div class="empty-state">
                <i data-lucide="loader" class="animate-spin"></i>
                <p>Loading task history logs...</p>
            </div>
        `;
        lucide.createIcons();

        fetch("/api/tasks")
            .then(res => res.json())
            .then(data => {
                allTasks = data;
                renderTaskList(allTasks);
            })
            .catch(err => {
                taskListContainer.innerHTML = `
                    <div class="error-banner">
                        <i data-lucide="alert-circle"></i>
                        <p>Error loading tasks: ${err.message || err}</p>
                    </div>
                `;
                lucide.createIcons();
            });
    }

    function renderTaskList(tasks) {
        taskListContainer.innerHTML = "";
        if (tasks.length === 0) {
            taskListContainer.innerHTML = `
                <div class="empty-state">
                    <i data-lucide="folder-open"></i>
                    <p>No tasks found.</p>
                </div>
            `;
            lucide.createIcons();
            return;
        }

        tasks.forEach(t => {
            const btn = document.createElement("button");
            btn.className = `task-item-btn ${selectedTaskId === t.task_id ? "active" : ""}`;
            btn.setAttribute("aria-label", `Task ${t.task_id}, Goal: ${t.goal}, Cost: $${t.total_spent.toFixed(4)}`);
            btn.innerHTML = `
                <div class="task-item-header">
                    <span>${t.task_id}</span>
                    <span class="task-item-cost">$${t.total_spent.toFixed(4)}</span>
                </div>
                <div class="task-item-goal">${t.goal}</div>
            `;
            btn.addEventListener("click", () => {
                selectedTaskId = t.task_id;
                document.querySelectorAll(".task-item-btn").forEach(b => b.classList.remove("active"));
                btn.classList.add("active");
                fetchTaskDetails(t.task_id);
            });
            taskListContainer.appendChild(btn);
        });
    }

    taskSearchInput.addEventListener("input", () => {
        const q = taskSearchInput.value.toLowerCase();
        const filtered = allTasks.filter(t => t.task_id.toLowerCase().includes(q) || t.goal.toLowerCase().includes(q));
        renderTaskList(filtered);
    });

    function fetchTaskDetails(task_id) {
        taskDetailsPane.innerHTML = `
            <div class="empty-state">
                <i data-lucide="loader" class="animate-spin"></i>
                <p>Loading task details...</p>
            </div>
        `;
        lucide.createIcons();

        fetch(`/api/task?id=${task_id}`)
            .then(res => res.json())
            .then(data => {
                // Checklist html
                let checklistHtml = "";
                if (data.checklist && data.checklist.length > 0) {
                    checklistHtml = `
                        <div class="glass-card">
                            <div class="card-header">
                                <h2>Verification Checklist</h2>
                                <i data-lucide="check-square" aria-hidden="true"></i>
                            </div>
                            <div class="card-body checklist-container">
                                ${data.checklist.map(item => `
                                    <div class="checklist-item ${item.status === "completed" ? "completed" : ""}">
                                        ${item.status === "completed" 
                                            ? `<i data-lucide="check-circle-2" class="check-icon" aria-hidden="true"></i>` 
                                            : `<i data-lucide="circle" class="pending-icon" aria-hidden="true"></i>`}
                                        <span>${item.criterion}</span>
                                    </div>
                                `).join("")}
                            </div>
                        </div>
                    `;
                } else {
                    checklistHtml = `
                        <div class="glass-card">
                            <div class="card-header">
                                <h2>Verification Checklist</h2>
                                <i data-lucide="check-square" aria-hidden="true"></i>
                            </div>
                            <div class="card-body">
                                <p class="stat-sub">No checklist gates configured for this task.</p>
                            </div>
                        </div>
                    `;
                }

                // Envelope Front-matter html
                const envelopeHtml = `
                    <div class="glass-card">
                        <div class="card-header">
                            <h2>Scenario Front-Matter (L1-L3)</h2>
                            <i data-lucide="file-text" aria-hidden="true"></i>
                        </div>
                        <div class="card-body controls-list">
                            <div class="control-row">
                                <span class="control-name"><b>System & Invariants (L1):</b></span>
                            </div>
                            <pre style="background: rgba(0,0,0,0.3); padding: 8px; border-radius: 8px; font-size: 0.8rem; overflow: auto; max-height: 120px; white-space: pre-wrap; font-family: 'Fira Code', monospace;">${data.envelope.layer1}</pre>
                            
                            <div class="control-row">
                                <span class="control-name"><b>Decisions & Questions (L2):</b></span>
                            </div>
                            <pre style="background: rgba(0,0,0,0.3); padding: 8px; border-radius: 8px; font-size: 0.8rem; overflow: auto; max-height: 120px; white-space: pre-wrap; font-family: 'Fira Code', monospace;">${data.envelope.layer2}</pre>
                        </div>
                    </div>
                `;

                // Turns history HTML
                let turnsHtml = "";
                if (data.transcripts && data.transcripts.length > 0) {
                    turnsHtml = `
                        <div class="glass-card" style="grid-column: span 2;">
                            <div class="card-header">
                                <h2>Transcript Execution Turns (L0)</h2>
                                <i data-lucide="message-square" aria-hidden="true"></i>
                            </div>
                            <div class="card-body chat-turns-history">
                                ${data.transcripts.map(turn => `
                                    <div class="message-turn ${turn.role}">
                                        <div class="message-meta">
                                            <span>${turn.role.toUpperCase()} (Index ${turn.turn_index})</span>
                                            ${turn.cost > 0 ? `<span class="cost-tag">$${turn.cost.toFixed(5)}</span>` : ""}
                                        </div>
                                        <div class="message-bubble">${turn.content}</div>
                                    </div>
                                `).join("")}
                            </div>
                        </div>
                    `;
                }

                taskDetailsPane.innerHTML = `
                    <div class="detail-header">
                        <h2>Goal: ${data.goal}</h2>
                        <div class="detail-meta-row">
                            <span>Task ID: <strong>${data.task_id}</strong></span>
                            <span>Spent: <strong class="cost-tag" style="margin: 0; font-size: 0.85rem;">$${data.total_spent.toFixed(4)}</strong></span>
                        </div>
                    </div>
                    <div class="pane-grid">
                        ${checklistHtml}
                        ${envelopeHtml}
                        ${turnsHtml}
                    </div>
                `;
                lucide.createIcons();
            })
            .catch(err => {
                taskDetailsPane.innerHTML = `
                    <div class="error-banner">
                        <i data-lucide="alert-circle"></i>
                        <p>Error loading task details: ${err.message || err}</p>
                    </div>
                `;
                lucide.createIcons();
            });
    }

    // ========================================================
    // Codebase Graph Drawer (Canvas Force Simulation)
    // ========================================================
    let canvasWidth, canvasHeight;
    let scale = 1;
    let dragNode = null;
    let offset = { x: 0, y: 0 };
    let hoverNode = null;

    function fetchGraph() {
        // Draw loading state on canvas
        resizeCanvas();
        graphCtx.clearRect(0, 0, canvasWidth, canvasHeight);
        graphCtx.font = "14px 'Fira Sans', sans-serif";
        graphCtx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--text-dim').trim() || "#82828c";
        graphCtx.textAlign = "center";
        graphCtx.fillText("Loading Codebase Imports Graph...", canvasWidth / 2, canvasHeight / 2);

        fetch("/api/graph")
            .then(res => res.json())
            .then(data => {
                graphData = data;
                initGraphPhysics();
            })
            .catch(err => {
                graphCtx.clearRect(0, 0, canvasWidth, canvasHeight);
                graphCtx.font = "14px 'Fira Sans', sans-serif";
                graphCtx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--accent-red').trim() || "#ef4444";
                graphCtx.textAlign = "center";
                graphCtx.fillText("Failed to load dependency graph: " + (err.message || err), canvasWidth / 2, canvasHeight / 2);
            });
    }

    function initGraphPhysics() {
        resizeCanvas();
        
        // Initialize positions if not set
        graphData.nodes.forEach((n, idx) => {
            if (!n.x) {
                // Circular layout init
                const angle = (idx / graphData.nodes.length) * Math.PI * 2;
                const radius = Math.min(canvasWidth, canvasHeight) * 0.3;
                n.x = canvasWidth / 2 + Math.cos(angle) * radius;
                n.y = canvasHeight / 2 + Math.sin(angle) * radius;
                n.vx = 0;
                n.vy = 0;
            }
        });

        // Set index lookup maps for nodes
        const nodeMap = {};
        graphData.nodes.forEach(n => {
            nodeMap[n.filename] = n;
        });

        graphData.edges.forEach(e => {
            e.sourceNode = nodeMap[e.parent];
            e.targetNode = nodeMap[e.child];
        });

        if (!forceSimulationActive) {
            forceSimulationActive = true;
            requestAnimationFrame(updateGraphPhysics);
        }
    }

    function resizeCanvas() {
        const rect = graphCanvas.parentElement.getBoundingClientRect();
        graphCanvas.width = rect.width;
        graphCanvas.height = rect.height;
        canvasWidth = rect.width;
        canvasHeight = rect.height;
    }

    function updateGraphPhysics() {
        if (!forceSimulationActive || activePanel !== "panel-graph") {
            forceSimulationActive = false;
            return;
        }

        // Apply Forces
        const kRepulsion = 1500;
        const kAttraction = 0.05;
        const gravity = 0.02;

        // 1. Repulsion between all nodes
        for (let i = 0; i < graphData.nodes.length; i++) {
            const n1 = graphData.nodes[i];
            for (let j = i + 1; j < graphData.nodes.length; j++) {
                const n2 = graphData.nodes[j];
                const dx = n2.x - n1.x;
                const dy = n2.y - n1.y;
                const distSq = dx * dx + dy * dy + 1;
                const dist = Math.sqrt(distSq);
                
                if (dist < 300) {
                    const force = kRepulsion / distSq;
                    const fx = (dx / dist) * force;
                    const fy = (dy / dist) * force;
                    
                    if (n1 !== dragNode) {
                        n1.vx -= fx;
                        n1.vy -= fy;
                    }
                    if (n2 !== dragNode) {
                        n2.vx += fx;
                        n2.vy += fy;
                    }
                }
            }
        }

        // 2. Attraction along edges
        graphData.edges.forEach(e => {
            if (e.sourceNode && e.targetNode) {
                const s = e.sourceNode;
                const t = e.targetNode;
                const dx = t.x - s.x;
                const dy = t.y - s.y;
                const dist = Math.sqrt(dx * dx + dy * dy) || 1;
                
                const force = kAttraction * (dist - 100);
                const fx = (dx / dist) * force;
                const fy = (dy / dist) * force;
                
                if (s !== dragNode) {
                    s.vx += fx;
                    s.vy += fy;
                }
                if (t !== dragNode) {
                    t.vx -= fx;
                    t.vy -= fy;
                }
            }
        });

        // 3. Gravity pulling to center and drag update
        graphData.nodes.forEach(n => {
            if (n === dragNode) return;

            const dx = canvasWidth / 2 - n.x;
            const dy = canvasHeight / 2 - n.y;
            n.vx += dx * gravity;
            n.vy += dy * gravity;

            // Apply friction and cap velocity
            n.vx *= 0.85;
            n.vy *= 0.85;
            
            n.x += n.vx;
            n.y += n.vy;
        });

        // Draw Graph
        drawGraph();

        requestAnimationFrame(updateGraphPhysics);
    }

    function drawGraph() {
        graphCtx.clearRect(0, 0, canvasWidth, canvasHeight);
        graphCtx.save();
        
        // Get computed style colors to support CSS theme variables in Canvas
        const colorBlue = getComputedStyle(document.documentElement).getPropertyValue('--accent-blue').trim() || "#e0a300";
        const colorRed = getComputedStyle(document.documentElement).getPropertyValue('--accent-red').trim() || "#ef4444";
        const colorAmber = getComputedStyle(document.documentElement).getPropertyValue('--accent-amber').trim() || "#FFC321";
        const colorTextPrimary = getComputedStyle(document.documentElement).getPropertyValue('--text-primary').trim() || "#f4f4f6";

        // Draw Edges
        graphCtx.strokeStyle = "rgba(255,255,255,0.06)";
        graphCtx.lineWidth = 1.5;
        graphData.edges.forEach(e => {
            if (e.sourceNode && e.targetNode) {
                graphCtx.beginPath();
                graphCtx.moveTo(e.sourceNode.x, e.sourceNode.y);
                graphCtx.lineTo(e.targetNode.x, e.targetNode.y);
                graphCtx.stroke();
            }
        });

        // Draw Nodes
        graphData.nodes.forEach(n => {
            let color = colorBlue;
            let shadow = "rgba(224, 163, 0, 0.4)";
            let radius = 10;

            if (n.is_tagged) {
                color = colorRed;
                shadow = "rgba(239, 68, 68, 0.6)";
                radius = 12;
            } else if (n.reaches_tagged) {
                color = colorAmber;
                shadow = "rgba(255, 195, 33, 0.6)";
                radius = 11;
            }

            // Outer Glow shadow
            graphCtx.shadowColor = shadow;
            graphCtx.shadowBlur = (hoverNode === n) ? 15 : 6;
            
            graphCtx.fillStyle = color;
            graphCtx.beginPath();
            graphCtx.arc(n.x, n.y, radius, 0, Math.PI * 2);
            graphCtx.fill();
            
            // Reset shadows for text
            graphCtx.shadowBlur = 0;

            // Labels
            if (hoverNode === n || n.is_tagged || graphData.nodes.length < 30) {
                graphCtx.fillStyle = colorTextPrimary;
                graphCtx.font = (hoverNode === n) ? "bold 11px 'Fira Sans'" : "10px 'Fira Sans'";
                graphCtx.textAlign = "center";
                
                // Show short relative filename basename
                const basename = n.filename.split("/").pop();
                graphCtx.fillText(basename, n.x, n.y - radius - 6);
            }
        });

        graphCtx.restore();
    }

    // Graph interactions
    graphCanvas.addEventListener("mousedown", e => {
        const rect = graphCanvas.getBoundingClientRect();
        const mx = e.clientX - rect.left;
        const my = e.clientY - rect.top;

        // Check if clicked node
        let clicked = null;
        for (let n of graphData.nodes) {
            const dist = Math.hypot(n.x - mx, n.y - my);
            if (dist < 15) {
                clicked = n;
                break;
            }
        }

        if (clicked) {
            dragNode = clicked;
        }
    });

    window.addEventListener("mouseup", () => {
        dragNode = null;
    });

    graphCanvas.addEventListener("mousemove", e => {
        const rect = graphCanvas.getBoundingClientRect();
        const mx = e.clientX - rect.left;
        const my = e.clientY - rect.top;

        if (dragNode) {
            dragNode.x = mx;
            dragNode.y = my;
            dragNode.vx = 0;
            dragNode.vy = 0;
        }

        // Hover checking
        let hover = null;
        for (let n of graphData.nodes) {
            const dist = Math.hypot(n.x - mx, n.y - my);
            if (dist < 15) {
                hover = n;
                break;
            }
        }
        hoverNode = hover;
    });

    btnReindexGraph.addEventListener("click", () => {
        btnReindexGraph.disabled = true;
        btnReindexGraph.innerHTML = `<i data-lucide="loader" class="animate-spin"></i> Reindexing...`;
        lucide.createIcons();

        // Canvas loading state during reindexing
        graphCtx.clearRect(0, 0, canvasWidth, canvasHeight);
        graphCtx.font = "14px 'Fira Sans', sans-serif";
        graphCtx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--accent-amber').trim() || "#FFC321";
        graphCtx.textAlign = "center";
        graphCtx.fillText("Reindexing codebase imports graph...", canvasWidth / 2, canvasHeight / 2);
        
        fetch("/api/graph")
            .then(res => res.json())
            .then(data => {
                graphData = data;
                initGraphPhysics();
                btnReindexGraph.disabled = false;
                btnReindexGraph.innerHTML = `<i data-lucide="refresh-cw"></i> Reindex Codebase`;
                lucide.createIcons();
            })
            .catch(err => {
                btnReindexGraph.disabled = false;
                btnReindexGraph.innerHTML = `<i data-lucide="refresh-cw"></i> Reindex Codebase`;
                lucide.createIcons();
                graphCtx.clearRect(0, 0, canvasWidth, canvasHeight);
                graphCtx.font = "14px 'Fira Sans', sans-serif";
                graphCtx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--accent-red').trim() || "#ef4444";
                graphCtx.textAlign = "center";
                graphCtx.fillText("Reindexing failed: " + (err.message || err), canvasWidth / 2, canvasHeight / 2);
            });
    });

    window.addEventListener("resize", () => {
        if (activePanel === "panel-graph") {
            resizeCanvas();
        }
    });

    // ========================================================
    // Chat Playground Controls
    // ========================================================
    btnChatSend.addEventListener("click", sendPlaygroundMessage);
    txtChatInput.addEventListener("keydown", e => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendPlaygroundMessage();
        }
    });

    function sendPlaygroundMessage() {
        const prompt = txtChatInput.value.trim();
        if (!prompt && !activeTaskId) return;

        // Clear input field
        txtChatInput.value = "";
        
        // Append user turn visually
        if (prompt) {
            appendMessage("user", prompt);
        }

        btnChatSend.disabled = true;
        btnChatSend.innerHTML = `<i data-lucide="loader" class="animate-spin"></i> Sending...`;
        lucide.createIcons();

        // Perform fetch run
        executePlaygroundTurn(prompt, false);
    }

    function executePlaygroundTurn(prompt, userConfirmed = false) {
        // Show a loading typing turn placeholder
        const typingDiv = document.createElement("div");
        typingDiv.className = "message-turn assistant typing-placeholder";
        typingDiv.innerHTML = `
            <div class="message-meta">ASSISTANT</div>
            <div class="message-bubble"><i data-lucide="loader" class="animate-spin" style="width: 14px; height: 14px; margin-right: 6px;"></i> Reasoning on scenario parameters...</div>
        `;
        playgroundChatHistory.appendChild(typingDiv);
        playgroundChatHistory.scrollTop = playgroundChatHistory.scrollHeight;
        lucide.createIcons();

        const body = {
            prompt: prompt,
            task_id: activeTaskId,
            user_confirmed: userConfirmed
        };

        fetch("/api/playground/run", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
        })
        .then(res => res.json())
        .then(data => {
            // Remove typing indicator
            const typingEl = playgroundChatHistory.querySelector(".typing-placeholder");
            if (typingEl) typingEl.remove();

            btnChatSend.disabled = false;
            btnChatSend.innerHTML = `<i data-lucide="send"></i> Send`;
            lucide.createIcons();

            if (data.status === "success") {
                activeTaskId = data.task_id;
                
                // Show close task bar
                playgroundCloseBar.classList.remove("hidden");
                lblActiveTaskId.innerText = `Active: ${data.task_id}`;

                // Repopulate whole chat history
                renderChatHistory(data.transcripts);
            } else if (data.status === "endgame") {
                // Capture active task_id if spawned
                if (data.task_id) {
                    activeTaskId = data.task_id;
                }
                
                // Show Endgame Modal Dialog warning
                endgameModalMsg.innerText = data.message;
                endgameModal.classList.remove("hidden");
            } else {
                appendMessage("system", `Error: ${data.error || "Execution failed"}`);
            }
        })
        .catch(err => {
            const typingEl = playgroundChatHistory.querySelector(".typing-placeholder");
            if (typingEl) typingEl.remove();

            btnChatSend.disabled = false;
            btnChatSend.innerHTML = `<i data-lucide="send"></i> Send`;
            lucide.createIcons();
            appendMessage("system", `Network Error: ${err.message || err}`);
        });
    }

    function renderChatHistory(transcripts) {
        playgroundChatHistory.innerHTML = "";
        transcripts.forEach(turn => {
            appendMessage(turn.role, turn.content, turn.cost, turn.model, turn.tier);
        });
    }

    function appendMessage(role, content, cost = null, model = null, tier = null) {
        if (role === "tool") {
            let toolName = "Unknown Tool";
            let toolArgs = {};
            let toolResult = "";
            try {
                const parsed = JSON.parse(content);
                toolName = parsed.name || toolName;
                toolArgs = parsed.args || toolArgs;
                toolResult = parsed.result || toolResult;
            } catch (e) {
                toolResult = content;
            }
            
            const turnDiv = document.createElement("div");
            turnDiv.className = "message-turn tool-call-msg";
            turnDiv.style.width = "100%";
            turnDiv.innerHTML = `
                <details class="tool-call-details" style="margin: 0.5rem 0; padding: 0.5rem; background: rgba(255,255,255,0.03); border-radius: 4px; border: 1px solid rgba(255,255,255,0.08);">
                    <summary style="cursor: pointer; font-size: 11px; font-weight: bold; color: var(--accent-blue);">
                        <i data-lucide="wrench" style="display:inline-block; width:12px; height:12px; margin-right:4px; vertical-align:middle;"></i>
                        Tool Call: ${toolName} <span style="font-weight: normal; color: var(--text-dim); margin-left: 8px;">(Cost: $0.00000)</span>
                    </summary>
                    <div style="margin-top: 0.5rem; font-size: 10px; font-family: monospace; white-space: pre-wrap; background: rgba(0,0,0,0.2); padding: 0.5rem; border-radius: 3px; color: var(--text-dim);">
                        <strong>Arguments:</strong> ${escapeHtml(JSON.stringify(toolArgs, null, 2))}
                        <hr style="border: 0; border-top: 1px solid rgba(255,255,255,0.08); margin: 0.5rem 0;" />
                        <strong>Result:</strong> ${escapeHtml(toolResult)}
                    </div>
                </details>
            `;
            playgroundChatHistory.appendChild(turnDiv);
            lucide.createIcons();
            playgroundChatHistory.scrollTop = playgroundChatHistory.scrollHeight;
            return;
        }

        const turnDiv = document.createElement("div");
        turnDiv.className = `message-turn ${role}`;

        let metaContent = role.toUpperCase();
        // Audit badge: exactly which model and tier produced this message
        if (model) {
            metaContent += ` <span class="model-badge" title="Model that produced this reply">${model}</span>`;
        }
        if (tier) {
            metaContent += ` <span class="tier-badge tier-${tier}">${tier}</span>`;
        }
        if (cost !== null && cost > 0) {
            metaContent += ` <span class="cost-tag">$${cost.toFixed(5)}</span>`;
        }

        turnDiv.innerHTML = `
            <div class="message-meta">${metaContent}</div>
            <div class="message-bubble">${content}</div>
        `;
        playgroundChatHistory.appendChild(turnDiv);
        playgroundChatHistory.scrollTop = playgroundChatHistory.scrollHeight;
    }

    // Modal Confirmation Controls
    btnModalConfirm.addEventListener("click", () => {
        endgameModal.classList.add("hidden");
        btnChatSend.disabled = true;
        btnChatSend.innerHTML = `<i data-lucide="loader" class="animate-spin"></i> Authorizing...`;
        lucide.createIcons();
        
        // Execute again with user confirmation
        executePlaygroundTurn("", true);
    });

    btnModalCancel.addEventListener("click", () => {
        endgameModal.classList.add("hidden");
        appendMessage("system", "Thinker spend rejected. Task execution aborted.");
        closeActiveTask();
    });

    // Close task action trigger
    btnChatClose.addEventListener("click", closeActiveTask);

    function closeActiveTask() {
        if (!activeTaskId) return;

        btnChatClose.disabled = true;
        btnChatClose.innerHTML = `<i data-lucide="loader" class="animate-spin"></i> Saving Handoff...`;
        lucide.createIcons();

        // Retrieve last used tier
        let last_tier = "explorer";

        fetch("/api/playground/close", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                task_id: activeTaskId,
                last_tier: last_tier,
            })
        })
        .then(res => res.json())
        .then(data => {
            btnChatClose.disabled = false;
            btnChatClose.innerHTML = `<i data-lucide="check-square"></i> Close Task & Commit Git`;
            lucide.createIcons();

            if (data.status === "success") {
                appendMessage("system", "Task closed successfully. Front-Matter handoff generated, and git scenario merge committed.");
                
                // Reset session
                activeTaskId = null;
                playgroundCloseBar.classList.add("hidden");
                lblActiveTaskId.innerText = "Active: None";
                
                // Refresh overview stats
                fetchStats();
            } else {
                appendMessage("system", `Closing failed: ${data.error}`);
            }
        })
        .catch(err => {
            btnChatClose.disabled = false;
            btnChatClose.innerHTML = `<i data-lucide="check-square"></i> Close Task & Commit Git`;
            lucide.createIcons();
            appendMessage("system", `Network Error: ${err.message || err}`);
        });
    }

    // ========================================================
    // API Fetch Keys Config
    // ========================================================
    const keyAnthropic = document.getElementById("key-anthropic");
    const keyGemini = document.getElementById("key-gemini");
    const keyOpenai = document.getElementById("key-openai");
    const inputWalletBudget = document.getElementById("input-wallet-budget");
    const btnSaveKeys = document.getElementById("btn-save-keys");
    const keysStatusMsg = document.getElementById("keys-status-msg");
    const customKeysList = document.getElementById("custom-keys-list");
    const btnAddCustomKey = document.getElementById("btn-add-custom-key");

    function addCustomKeyRow(name = "", placeholder = "API Key") {
        const row = document.createElement("div");
        row.className = "custom-key-row";
        
        row.innerHTML = `
            <input type="text" placeholder="e.g. DEEPSEEK" class="form-input custom-key-name" value="${name}">
            <input type="password" placeholder="${placeholder}" class="form-input custom-key-val">
            <button type="button" class="btn-remove-custom-key" aria-label="Remove custom key">
                <i data-lucide="trash-2"></i>
            </button>
        `;

        row.querySelector(".btn-remove-custom-key").addEventListener("click", () => {
            row.remove();
        });

        customKeysList.appendChild(row);
        lucide.createIcons();
    }

    if (btnAddCustomKey) {
        btnAddCustomKey.addEventListener("click", () => {
            addCustomKeyRow("", "API Key");
        });
    }

    function fetchKeys() {
        fetch("/api/keys")
            .then(res => res.json())
            .then(data => {
                if (data.anthropic_key_set) {
                    keyAnthropic.placeholder = "Configured: " + data.anthropic_masked;
                } else {
                    keyAnthropic.placeholder = "sk-ant-...";
                }
                if (data.gemini_key_set) {
                    keyGemini.placeholder = "Configured: " + data.gemini_masked;
                } else {
                    keyGemini.placeholder = "AIzaSy...";
                }
                if (data.openai_key_set) {
                    keyOpenai.placeholder = "Configured: " + data.openai_masked;
                } else {
                    keyOpenai.placeholder = "sk-proj-...";
                }
                
                // Populate Wallet Budget input field
                if (inputWalletBudget) {
                    inputWalletBudget.value = data.total_budget || 75.00;
                }

                // Reset inputs
                keyAnthropic.value = "";
                keyGemini.value = "";
                keyOpenai.value = "";

                // Populate custom keys
                customKeysList.innerHTML = "";
                if (data.custom_keys && data.custom_keys.length > 0) {
                    data.custom_keys.forEach(k => {
                        addCustomKeyRow(k.name, "Configured: " + k.masked);
                    });
                }
            })
            .catch(err => console.error("Error fetching keys:", err));
    }

    btnSaveKeys.addEventListener("click", () => {
        // Collect custom keys
        const customKeys = {};
        const rows = customKeysList.querySelectorAll(".custom-key-row");
        rows.forEach(row => {
            const name = row.querySelector(".custom-key-name").value.trim();
            const val = row.querySelector(".custom-key-val").value.trim();
            if (name) {
                customKeys[name] = val;
            }
        });

        const body = {
            anthropic_key: keyAnthropic.value.trim(),
            gemini_key: keyGemini.value.trim(),
            openai_key: keyOpenai.value.trim(),
            custom_keys: customKeys,
            total_budget: inputWalletBudget ? parseFloat(inputWalletBudget.value) : null
        };

        btnSaveKeys.disabled = true;
        btnSaveKeys.innerHTML = `<i data-lucide="save"></i> Saving...`;
        lucide.createIcons();

        fetch("/api/keys", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
        })
        .then(res => res.json())
        .then(data => {
            btnSaveKeys.disabled = false;
            btnSaveKeys.innerHTML = `<i data-lucide="save"></i> Save & Load Keys`;
            lucide.createIcons();

            if (data.status === "success") {
                keysStatusMsg.innerText = "Keys and wallet budget saved successfully!";
                keysStatusMsg.style.color = "var(--accent-green)";
                fetchKeys();
                fetchStats(); // Immediately update the overview metrics cards and headers
                setTimeout(() => {
                    keysStatusMsg.innerText = "";
                }, 3000);
            } else {
                keysStatusMsg.innerText = "Error: " + data.error;
                keysStatusMsg.style.color = "var(--accent-red)";
            }
        })
        .catch(err => {
            btnSaveKeys.disabled = false;
            btnSaveKeys.innerHTML = `<i data-lucide="save"></i> Save & Load Keys`;
            lucide.createIcons();
            keysStatusMsg.innerText = "Network error: " + (err.message || err);
            keysStatusMsg.style.color = "var(--accent-red)";
        });
    });

    // ========================================================
    // Model Configurations & Probe Suite
    // ========================================================
    const selectThinkerProv = document.getElementById("select-thinker-prov");
    const selectThinker = document.getElementById("select-thinker");
    const selectExplorerProv = document.getElementById("select-explorer-prov");
    const selectExplorer = document.getElementById("select-explorer");
    const selectCheapProv = document.getElementById("select-cheap-prov");
    const selectCheap = document.getElementById("select-cheap");
    
    const selectThinkerEffort = document.getElementById("select-thinker-effort");
    const selectExplorerEffort = document.getElementById("select-explorer-effort");
    
    const costThinkerEst = document.getElementById("cost-thinker-est");
    const costExplorerEst = document.getElementById("cost-explorer-est");
    const costCheapEst = document.getElementById("cost-cheap-est");
    
    const inputProbeModel = document.getElementById("input-probe-model");
    const btnRunProbe = document.getElementById("btn-run-probe");
    const probeResultsContainer = document.getElementById("probe-results-container");
    
    const btnSaveModelsConfig = document.getElementById("btn-save-models-config");
    const modelsStatusMsg = document.getElementById("models-status-msg");
 
    let modelsConfig = {
        available_models: [],
        pricing: {},
        tier_models: {},
        tier_thinking: {},
        custom_providers: {}
    };
 
    function fetchModelsConfig() {
        fetch("/api/models/config")
            .then(res => res.json())
            .then(data => {
                modelsConfig = data;
                populateProviderDropdowns();
                syncUIWithConfig();
            })
            .catch(err => {
                console.error("Error fetching model config:", err);
                if (modelsStatusMsg) {
                    modelsStatusMsg.innerText = "⚠ Could not load model config (" + err.message + "). If you recently updated the code, restart the dashboard server and hard-refresh this page.";
                    modelsStatusMsg.className = "status-msg error";
                }
            });
    }
 
    function populateProviderDropdowns() {
        const provSelects = [selectThinkerProv, selectExplorerProv, selectCheapProv];
        provSelects.forEach(select => {
            if (!select) return;
            select.innerHTML = `
                <option value="anthropic">Anthropic (Claude)</option>
                <option value="openai">OpenAI (GPT)</option>
                <option value="gemini">Google GenAI (Gemini)</option>
            `;
            const custom = modelsConfig.custom_providers || {};
            Object.keys(custom).forEach(cp => {
                const opt = document.createElement("option");
                opt.value = cp;
                opt.innerText = cp.charAt(0).toUpperCase() + cp.slice(1);
                select.appendChild(opt);
            });
        });
    }

    function syncUIWithConfig() {
        // Mapped values
        const thinkerModel = modelsConfig.tier_models.thinker || "claude-fable-5";
        const explorerModel = modelsConfig.tier_models.explorer || "claude-sonnet-5";
        const cheapModel = modelsConfig.tier_models.cheap || "claude-haiku-4-5-20251001";

        // Determine provider for each model from the pricing dict
        const getProvOfModel = (modelName, def) => {
            const mInfo = modelsConfig.pricing[modelName];
            return mInfo ? mInfo.provider : def;
        };

        const thinkerProv = getProvOfModel(thinkerModel, "anthropic");
        const explorerProv = getProvOfModel(explorerModel, "anthropic");
        const cheapProv = getProvOfModel(cheapModel, "anthropic");

        if (selectThinkerProv) selectThinkerProv.value = thinkerProv;
        if (selectExplorerProv) selectExplorerProv.value = explorerProv;
        if (selectCheapProv) selectCheapProv.value = cheapProv;

        // Populate and select models
        populateModelsForTier("thinker", thinkerProv, thinkerModel);
        populateModelsForTier("explorer", explorerProv, explorerModel);
        populateModelsForTier("cheap", cheapProv, cheapModel);

        populateThinkingEfforts();
    }

    function populateModelsForTier(tier, provider, activeVal) {
        let select, costEst;
        if (tier === "thinker") { select = selectThinker; costEst = costThinkerEst; }
        else if (tier === "explorer") { select = selectExplorer; costEst = costExplorerEst; }
        else { select = selectCheap; costEst = costCheapEst; }

        if (!select) return;
        select.innerHTML = "";

        // Filter models config by provider
        const models = modelsConfig.available_models || [];
        const filtered = models.filter(m => {
            const mInfo = modelsConfig.pricing[m];
            return mInfo && mInfo.provider === provider;
        });

        filtered.forEach(m => {
            const opt = document.createElement("option");
            opt.value = m;
            opt.innerText = m;
            select.appendChild(opt);
        });

        if (activeVal && filtered.includes(activeVal)) {
            select.value = activeVal;
        } else if (filtered.length > 0) {
            select.value = filtered[0];
        }

        updateCostEstimateForTier(tier);
    }

    function updateCostEstimateForTier(tier) {
        let select, costEst;
        if (tier === "thinker") { select = selectThinker; costEst = costThinkerEst; }
        else if (tier === "explorer") { select = selectExplorer; costEst = costExplorerEst; }
        else { select = selectCheap; costEst = costCheapEst; }

        if (!select || !costEst) return;

        const modelName = select.value;
        const mInfo = modelsConfig.pricing[modelName];
        if (mInfo) {
            // Compute a realistic estimate based on pricing details: 
            // 1.5M input + 0.5M output tokens per typical task turn
            const inputRate = mInfo.input || 0.0;
            const outputRate = mInfo.output || 0.0;
            const turnCost = (inputRate * 0.15) + (outputRate * 0.05); // per 100k input / 50k output
            costEst.innerText = `Est. cost: $${turnCost.toFixed(4)} / turn`;
        } else {
            costEst.innerText = "Est. cost: --";
        }
    }
 
    function populateThinkingEfforts() {
        const efforts = modelsConfig.tier_thinking || {};
        if (efforts.thinker && selectThinkerEffort) selectThinkerEffort.value = efforts.thinker;
        if (efforts.explorer && selectExplorerEffort) selectExplorerEffort.value = efforts.explorer;
    }

    // Event listeners
    if (selectThinkerProv) selectThinkerProv.addEventListener("change", (e) => populateModelsForTier("thinker", e.target.value));
    if (selectExplorerProv) selectExplorerProv.addEventListener("change", (e) => populateModelsForTier("explorer", e.target.value));
    if (selectCheapProv) selectCheapProv.addEventListener("change", (e) => populateModelsForTier("cheap", e.target.value));

    if (selectThinker) selectThinker.addEventListener("change", () => updateCostEstimateForTier("thinker"));
    if (selectExplorer) selectExplorer.addEventListener("change", () => updateCostEstimateForTier("explorer"));
    if (selectCheap) selectCheap.addEventListener("change", () => updateCostEstimateForTier("cheap"));

    if (btnSaveModelsConfig) {
        btnSaveModelsConfig.addEventListener("click", () => {
            modelsConfig.tier_models = {
                thinker: selectThinker.value,
                explorer: selectExplorer.value,
                cheap: selectCheap.value
            };
            modelsConfig.tier_thinking = {
                thinker: selectThinkerEffort.value,
                explorer: selectExplorerEffort.value,
                cheap: "low",
                leader: "low"
            };

            btnSaveModelsConfig.disabled = true;
            btnSaveModelsConfig.innerHTML = `<i data-lucide="save"></i> Saving...`;
            lucide.createIcons();

            fetch("/api/models/config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    tier_models: modelsConfig.tier_models,
                    tier_thinking: modelsConfig.tier_thinking
                })
            })
            .then(res => res.json())
            .then(data => {
                btnSaveModelsConfig.disabled = false;
                btnSaveModelsConfig.innerHTML = `<i data-lucide="save"></i> Save Model Configurations`;
                lucide.createIcons();

                if (data.status === "success") {
                    modelsStatusMsg.innerText = "Model configurations saved successfully!";
                    modelsStatusMsg.style.color = "var(--accent-green)";
                    fetchModelsConfig();
                    setTimeout(() => {
                        modelsStatusMsg.innerText = "";
                    }, 3000);
                } else {
                    modelsStatusMsg.innerText = "Error: " + data.error;
                    modelsStatusMsg.style.color = "var(--accent-red)";
                }
            })
            .catch(err => {
                btnSaveModelsConfig.disabled = false;
                btnSaveModelsConfig.innerHTML = `<i data-lucide="save"></i> Save Model Configurations`;
                lucide.createIcons();
                modelsStatusMsg.innerText = "Network error: " + (err.message || err);
                modelsStatusMsg.style.color = "var(--accent-red)";
            });
        });
    }

    if (btnRunProbe) {
        btnRunProbe.addEventListener("click", () => {
            const selectProbeProv = document.getElementById("select-probe-prov");
            const provider = selectProbeProv ? selectProbeProv.value : "openai";
            const model = inputProbeModel.value.trim();
            if (!model) {
                alert("Please enter a model name to run the probe suite.");
                return;
            }

            btnRunProbe.disabled = true;
            btnRunProbe.innerHTML = `<i data-lucide="loader" class="animate-spin"></i> Running Probe...`;
            lucide.createIcons();
            probeResultsContainer.classList.remove("hidden");
            probeResultsContainer.innerHTML = `<div class="stat-sub">Executing 6 objective validation tests. Please wait...</div>`;

            fetch("/api/models/probe", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ provider, model })
            })
            .then(res => res.json())
            .then(data => {
                btnRunProbe.disabled = false;
                btnRunProbe.innerHTML = `<i data-lucide="play"></i> Run Objective Benchmark`;
                lucide.createIcons();

                if (data.status === "success") {
                    probeResultsContainer.innerHTML = `
                        <div style="font-weight:600;margin-bottom:var(--space-xs);color:var(--text-primary)">
                            Benchmark Score: ${data.score}/${data.total_tasks} 
                            &mdash; Proposed Tier: <span class="cost-tag" style="background:var(--glass-border);color:var(--accent-cyan);">${data.proposed_tier.toUpperCase()}</span>
                            (<span style="color:var(--accent-violet);">${data.provisional_status}</span>)
                        </div>
                    `;
                    data.results.forEach(test => {
                        const item = document.createElement("div");
                        item.className = `probe-result-item ${test.passed ? 'passed' : 'failed'}`;
                        item.innerHTML = `
                            <span>Task ${test.id}: ${test.name}</span>
                            <span class="probe-result-status ${test.passed ? 'pass' : 'fail'}">${test.passed ? 'passed' : 'failed'}</span>
                        `;
                        probeResultsContainer.appendChild(item);
                    });
                    
                    // Refresh configs
                    fetchModelsConfig();
                } else {
                    probeResultsContainer.innerHTML = `<div class="error-banner" style="margin-top:0"><i data-lucide="alert-triangle"></i> Probe suite failed: ${data.error}</div>`;
                    lucide.createIcons();
                }
            })
            .catch(err => {
                btnRunProbe.disabled = false;
                btnRunProbe.innerHTML = `<i data-lucide="play"></i> Run Objective Benchmark`;
                lucide.createIcons();
                probeResultsContainer.innerHTML = `<div class="error-banner" style="margin-top:0"><i data-lucide="alert-triangle"></i> Network error: ${err.message || err}</div>`;
                lucide.createIcons();
            });
        });
    }

    // ========================================================
    // Council Chamber — live collaboration sessions
    // ========================================================
    const chamberGoal = document.getElementById("chamber-goal");
    const chamberBudget = document.getElementById("chamber-budget");
    const btnChamberStart = document.getElementById("btn-chamber-start");
    const chamberFeed = document.getElementById("chamber-feed");
    const chamberEmpty = document.getElementById("chamber-empty");
    const chamberCost = document.getElementById("chamber-cost");
    const chamberStatus = document.getElementById("chamber-status");

    let chamberTaskId = null;
    let chamberPollTimer = null;
    let chamberRenderedTurns = 0;

    function chamberSetStatus(text, kind) {
        if (!chamberStatus) return;
        chamberStatus.classList.remove("hidden");
        chamberStatus.className = "chamber-status " + (kind || "");
        chamberStatus.innerText = text;
    }

    function chamberAppend(turn) {
        const div = document.createElement("div");
        if (turn.role === "tool") {
            let toolName = "Unknown Tool";
            let toolArgs = {};
            let toolResult = "";
            try {
                const parsed = JSON.parse(turn.content);
                toolName = parsed.name || toolName;
                toolArgs = parsed.args || toolArgs;
                toolResult = parsed.result || toolResult;
            } catch (e) {
                toolResult = turn.content;
            }
            
            div.className = "chamber-msg tool-call-msg";
            div.style.width = "100%";
            div.innerHTML = `
                <details class="tool-call-details" style="margin: 0.5rem 0; padding: 0.5rem; background: rgba(255,255,255,0.03); border-radius: 4px; border: 1px solid rgba(255,255,255,0.08);">
                    <summary style="cursor: pointer; font-size: 11px; font-weight: bold; color: var(--accent-blue);">
                        <i data-lucide="wrench" style="display:inline-block; width:12px; height:12px; margin-right:4px; vertical-align:middle;"></i>
                        Tool Call: ${toolName} <span style="font-weight: normal; color: var(--text-dim); margin-left: 8px;">(Cost: $0.00000)</span>
                    </summary>
                    <div style="margin-top: 0.5rem; font-size: 10px; font-family: monospace; white-space: pre-wrap; background: rgba(0,0,0,0.2); padding: 0.5rem; border-radius: 3px; color: var(--text-dim);">
                        <strong>Arguments:</strong> ${escapeHtml(JSON.stringify(toolArgs, null, 2))}
                        <hr style="border: 0; border-top: 1px solid rgba(255,255,255,0.08); margin: 0.5rem 0;" />
                        <strong>Result:</strong> ${escapeHtml(toolResult)}
                    </div>
                </details>
            `;
            chamberFeed.appendChild(div);
            lucide.createIcons();
            chamberFeed.scrollTop = chamberFeed.scrollHeight;
            return;
            }

        if (turn.branch) {
            let container = chamberFeed.querySelector(".chamber-branches-container:last-of-type");
            if (!container) {
                container = document.createElement("div");
                container.className = "chamber-branches-container";
                container.style.display = "flex";
                container.style.gap = "1rem";
                container.style.width = "100%";
                container.style.margin = "1rem 0";
                chamberFeed.appendChild(container);
            }
            
            let branchCol = container.querySelector(`.branch-col-${turn.branch}`);
            if (!branchCol) {
                branchCol = document.createElement("div");
                branchCol.className = `branch-col branch-col-${turn.branch}`;
                branchCol.style.flex = "1";
                branchCol.style.minWidth = "0";
                branchCol.style.background = "rgba(255,255,255,0.02)";
                branchCol.style.border = "1px solid rgba(255,255,255,0.05)";
                branchCol.style.padding = "0.75rem";
                branchCol.style.borderRadius = "6px";
                
                const header = document.createElement("div");
                header.style.fontSize = "11px";
                header.style.fontWeight = "bold";
                header.style.color = "var(--accent-blue)";
                header.style.borderBottom = "1px solid rgba(255,255,255,0.08)";
                header.style.paddingBottom = "0.25rem";
                header.style.marginBottom = "0.5rem";
                header.innerText = `Branch ${turn.branch}`;
                branchCol.appendChild(header);

                if (turn.branch === "A") {
                    container.insertBefore(branchCol, container.firstChild);
                } else if (turn.branch === "B") {
                    const cCol = container.querySelector(".branch-col-C");
                    if (cCol) {
                        container.insertBefore(branchCol, cCol);
                    } else {
                        container.appendChild(branchCol);
                    }
                } else {
                    container.appendChild(branchCol);
                }
            }
            
            const bubbleDiv = document.createElement("div");
            bubbleDiv.className = `chamber-msg tier-${turn.tier || "unknown"}`;
            bubbleDiv.style.margin = "0.5rem 0";
            const meta = `<div class="message-meta">${(turn.tier || "model").toUpperCase()}` +
                (turn.model ? ` <span class="model-badge">${turn.model}</span>` : "") +
                (turn.cost ? ` <span class="cost-tag">$${Number(turn.cost).toFixed(5)}</span>` : "") +
                `</div>`;
            bubbleDiv.innerHTML = `${meta}<div class="message-bubble">${escapeHtml(turn.content)}</div>`;
            branchCol.appendChild(bubbleDiv);
            chamberFeed.scrollTop = chamberFeed.scrollHeight;
            return;
        }

        const tierClass = turn.tier ? `tier-${turn.tier}` : (turn.role === "system" ? "tier-system" : "tier-unknown");
        div.className = `chamber-msg ${tierClass}`;
        let meta = "";
        if (turn.role === "system") {
            div.classList.add("chamber-marker");
            div.innerText = turn.content;
        } else {
            meta = `<div class="message-meta">${(turn.tier || "model").toUpperCase()}` +
                (turn.model ? ` <span class="model-badge">${turn.model}</span>` : "") +
                (turn.cost ? ` <span class="cost-tag">$${Number(turn.cost).toFixed(5)}</span>` : "") +
                `</div>`;
            div.innerHTML = `${meta}<div class="message-bubble">${escapeHtml(turn.content)}</div>`;
        }
        chamberFeed.appendChild(div);
        chamberFeed.scrollTop = chamberFeed.scrollHeight;
    }

    function escapeHtml(str) {
        const d = document.createElement("div");
        d.innerText = str || "";
        return d.innerHTML;
    }

    function chamberPoll() {
        if (!chamberTaskId) return;
        fetch(`/api/task?id=${chamberTaskId}`)
            .then(r => r.json())
            .then(data => {
                const turns = data.transcripts || [];
                let cost = 0;
                turns.forEach((t, i) => {
                    if (t.cost) cost += t.cost;
                    if (i >= chamberRenderedTurns) chamberAppend(t);
                });
                chamberRenderedTurns = turns.length;
                chamberCost.innerText = `Session cost: $${cost.toFixed(5)}`;
            })
            .catch(() => {});

        fetch(`/api/collab/result?id=${chamberTaskId}`)
            .then(r => r.json())
            .then(res => {
                if (!res) return;
                
                // Handle awaiting_user status
                if (res.status === "awaiting_user") {
                    let inputContainer = document.getElementById("chamber-user-input-container");
                    if (!inputContainer) {
                        inputContainer = document.createElement("div");
                        inputContainer.id = "chamber-user-input-container";
                        inputContainer.className = "chamber-msg tier-system";
                        inputContainer.style.background = "rgba(var(--accent-blue-rgb), 0.1)";
                        inputContainer.style.border = "1px solid var(--accent-blue)";
                        inputContainer.style.padding = "1rem";
                        inputContainer.style.borderRadius = "6px";
                        inputContainer.style.margin = "1rem 0";
                        inputContainer.innerHTML = `
                            <div style="font-weight: bold; margin-bottom: 0.5rem; color: var(--accent-blue); font-size:12px;">
                                <i data-lucide="help-circle" style="display:inline-block; width:14px; height:14px; vertical-align:middle; margin-right:4px;"></i>
                                User Input Requested
                            </div>
                            <div id="chamber-user-question" style="font-size: 13px; margin-bottom: 1rem; color: var(--text-bright);"></div>
                            <div style="display: flex; gap: 0.5rem;">
                                <input type="text" id="chamber-user-answer" class="form-input" placeholder="Type your answer here..." style="flex: 1;" />
                                <button class="btn-send" id="btn-chamber-answer-submit">Submit</button>
                            </div>
                        `;
                        chamberFeed.appendChild(inputContainer);
                        lucide.createIcons();
                        
                        const btnSubmit = document.getElementById("btn-chamber-answer-submit");
                        const txtAnswer = document.getElementById("chamber-user-answer");
                        btnSubmit.addEventListener("click", () => {
                            const answer = txtAnswer.value.trim();
                            if (!answer) return;
                            btnSubmit.disabled = true;
                            fetch("/api/collab/answer", {
                                method: "POST",
                                headers: { "Content-Type": "application/json" },
                                body: JSON.stringify({ task_id: chamberTaskId, answer: answer })
                            })
                            .then(r => r.json())
                            .then(data => {
                                if (data.status === "success") {
                                    inputContainer.remove();
                                } else {
                                    btnSubmit.disabled = false;
                                    alert("Error: " + data.error);
                                }
                            })
                            .catch(err => {
                                btnSubmit.disabled = false;
                                alert("Network error: " + err);
                            });
                        });
                    }
                    document.getElementById("chamber-user-question").innerText = res.question || "";
                    chamberFeed.scrollTop = chamberFeed.scrollHeight;
                    return;
                } else {
                    const inputContainer = document.getElementById("chamber-user-input-container");
                    if (inputContainer) inputContainer.remove();
                }
                
                if (res.status === "running" || res.status === "starting") return;
                clearInterval(chamberPollTimer);
                chamberPollTimer = null;
                btnChamberStart.disabled = false;
                btnChamberStart.innerHTML = `<i data-lucide="play"></i> Convene`;
                lucide.createIcons();
                const kindMap = { approved: "ok", escalated: "warn", budget_halted: "err", ledger_halted: "err", api_error: "err", error: "err" };
                const label = {
                    approved: `APPROVED in ${res.rounds_used} round(s) — total $${(res.total_cost || 0).toFixed(4)}`,
                    escalated: `Not approved in ${res.rounds_used} rounds — escalated clean-room to Thinker`,
                    budget_halted: `Halted: session budget exhausted ($${(res.total_cost || 0).toFixed(4)} spent)`,
                    ledger_halted: `Halted by Ledger constraints`,
                    api_error: `Halted: repeated API failures — ${res.error || "check keys/model mapping for this tier"}`,
                    error: `Session error: ${res.error || "unknown"}`
                }[res.status] || res.status;
                chamberSetStatus(label, kindMap[res.status] || "");
            })
            .catch(() => {});
    }

    if (btnChamberStart) {
        btnChamberStart.addEventListener("click", () => {
            const goal = (chamberGoal.value || "").trim();
            if (!goal) { chamberSetStatus("Enter a goal first.", "err"); return; }
            if (chamberPollTimer) clearInterval(chamberPollTimer);

            chamberFeed.innerHTML = "";
            chamberRenderedTurns = 0;
            if (chamberEmpty) chamberEmpty.remove();
            chamberSetStatus("Convening the Council…", "");
            btnChamberStart.disabled = true;
            btnChamberStart.innerHTML = `<i data-lucide="loader" class="animate-spin"></i> In session…`;
            lucide.createIcons();

            const mode = document.getElementById("chamber-parallel")?.checked ? "tree" : "linear";
            fetch("/api/collab/run", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ goal: goal, budget: parseFloat(chamberBudget.value), mode: mode })
            })
                .then(r => r.json())
                .then(data => {
                    if (data.error) throw new Error(data.error);
                    chamberTaskId = data.task_id;
                    chamberSetStatus(`Session ${data.task_id} — budget $${data.budget.toFixed(2)}`, "");
                    chamberPollTimer = setInterval(chamberPoll, 2000);
                })
                .catch(err => {
                    chamberSetStatus(`Failed to start: ${err.message}`, "err");
                    btnChamberStart.disabled = false;
                    btnChamberStart.innerHTML = `<i data-lucide="play"></i> Convene`;
                    lucide.createIcons();
                });
        });
    }

    // Initial load
    fetchStats();
    fetchKeys();
    fetchModelsConfig();
});
