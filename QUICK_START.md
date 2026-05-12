# FlowCyt Quick Start Guide

## Running the Application

```bash
cd /sessions/wizardly-ecstatic-allen/mnt/flowcyt
python -m flowcyt.cli -i test_sample.fcs
```

Or without a file (will auto-load test_sample.fcs):
```bash
python -m flowcyt.cli
```

---

## Features Overview

### 1. **Polygon Gating (Fixed!)**
- **Select**: Polygon mode from Tool selector
- **Draw**: Left-click to add vertices
- **Preview**: See dotted line from last vertex to cursor
- **Closing preview**: See how polygon will close (when 2+ vertices)
- **Close**:
  - Double-click (after 3+ vertices)
  - Right-click
  - Press Enter key

### 2. **Ellipse Gating**
- **Select**: Ellipse mode from Tool selector
- **Draw**: Click for center, drag to define radius
- **Preview**: See red dashed ellipse while dragging
- **Release**: Let go to finalize

### 3. **Rectangle Gating**
- **Select**: Rectangle mode from Tool selector
- **Draw**: Click and drag
- **Preview**: See red dashed rectangle while dragging

### 4. **Sub-gating (Hierarchical Gates)**

**How it works:**
1. Create a parent gate first (any type: polygon, rectangle, or ellipse)
2. Look at the **"Parent Gate" selector** in the left panel (below the Tool selector)
3. Click on the parent gate name in the selector
4. You'll see a message: `[FlowCyt] Sub-gating: New gates will be children of 'R1'`
5. Create a new gate - it will automatically be a child of the selected parent
6. Child gates are **indented** in the statistics display

**Example workflow:**
```
1. Create rectangle R1 (selects all lymphocytes)
2. In "Parent Gate" selector, click "R1"
3. Create polygon P1 (selects CD4+ cells within lymphocytes)
4. P1 stats will show as subset of R1
```

**To go back to root-level gates:**
- Select "None" in the Parent Gate selector

### 5. **Auto Clustering**

**Requirements:**
```bash
pip install scikit-learn scipy
```

**How to use:**
1. Click **"Auto Cluster"** button
2. If dialog appears:
   - Choose DBSCAN (density-based) or KMeans (partition-based)
   - Enter parameters (epsilon for DBSCAN, or cluster count for KMeans)
3. If no dialog (dialog fails):
   - **Default**: KMeans with 3 clusters runs automatically
   - Check console for: `[FlowCyt] Created N gates from clustering`

### 6. **Summary Window**

- Click **"Summary"** button to see:
  - Bar chart of gate populations
  - Histograms for X and Y channels
  - Statistics table
- **Fixed**: Can now close and reopen without crashing!

### 7. **Open FCS Button**

- Click **"Open FCS"** button
- If file dialog doesn't appear:
  - **Auto-loads**: test_sample.fcs automatically
  - Check console for: `[FlowCyt] Auto-loading: test_sample.fcs`

---

## Console Messages Guide

Watch the console for helpful messages:

### Initialization
```
[FlowCyt] Welcome! Features:
  • Polygon: Preview lines + double-click to close
  • Ellipse: Click-drag for elliptical gates
  • Sub-gating: Select parent in 'Parent Gate' before creating child
  • Auto Cluster: Automatic gate creation (requires scikit-learn)
```

### Sub-gating Messages
```
[FlowCyt] Tip: Use 'Parent Gate' selector (left panel) to create sub-gates
[FlowCyt] Sub-gating: New gates will be children of 'R1'
[FlowCyt] Sub-gating: Creating root-level gates (no parent)
```

### Gate Creation
```
[FlowCyt] Gate 'R1': 12,456 events (45.23%)
[FlowCyt] Gate 'P1' (child of R1): 3,421 events (12.42%)
```

### Auto Clustering
```
[FlowCyt] Using default: KMeans with 3 clusters
[FlowCyt] Created 3 gates from clustering
```

---

## Troubleshooting

### "Auto Cluster does nothing"
**Solution**: Check console messages
- If you see `[FlowCyt] Clustering requires scikit-learn and scipy`, install dependencies:
  ```bash
  pip install scikit-learn scipy
  ```
- Otherwise, it should work with default settings (KMeans, 3 clusters)

### "Open FCS does nothing"
**Solution**: Check console for auto-loading message
- Should automatically load test_sample.fcs
- If not, run with: `python -m flowcyt.cli -i test_sample.fcs`

### "Summary window crashes"
**Fixed!** If still crashes:
- Check for error messages in console
- Try clicking Summary button again (old window is auto-closed now)

### "Sub-gating doesn't work"
**Check these:**
1. Is the "Parent Gate" selector visible? (Left panel, below Tool selector)
2. Did you create at least one gate first? (Parent selector appears after first gate)
3. Did you select a parent before creating the child gate?
4. Look for console message: `[FlowCyt] Sub-gating: New gates will be children of 'X'`
5. Child gates should be **indented** in the stats panel

### "Can't see parent gate selector"
- Create at least one gate first
- Look for console message: `[FlowCyt] Tip: Use 'Parent Gate' selector...`
- Selector is in left panel, below the Tool selector (Navigate/Polygon/Rectangle/Ellipse)

---

## UI Layout Reference

```
┌─────────────────────────────────────────────────┐
│ FlowCyt                                          │
├──────────┬──────────────────────────────────────┤
│          │                                       │
│ X Chan   │                                       │
│ ☐ FSC-A  │         Main Scatter Plot            │
│ ☐ SSC-A  │       (Density colored)              │
│ ☐ CD3    │                                       │
│          │                                       │
│ Y Chan   │                                       │
│ ☐ FSC-A  │                                       │
│ ☐ SSC-A  │                                       │
│ ☐ CD3    │                                       │
│          ├──────────────────────────────────────┤
│ Tool     │ Gate Statistics                      │
│ ⦿ Navigate  R1        12,456  (45.2%)          │
│ ◯ Polygon    P1        3,421  (12.4%) [child]  │
│ ◯ Rectangle  E1        2,100  (7.6%)           │
│ ◯ Ellipse │                                     │
│          │                                       │
│ Parent   │                                       │
│ ⦿ None   │                                       │
│ ◯ R1     │                                       │
│ ◯ P1     │                                       │
│          │                                       │
│[Clear]   │                                       │
│[Summary] │                                       │
│[Export]  │                                       │
│[Open]    │                                       │
│[AutoClust]                                      │
└──────────┴──────────────────────────────────────┘
```

---

## Keyboard Shortcuts

- **Enter**: Close polygon (when in Polygon mode)
- **Pan/Zoom**: Available in Navigate mode

---

## Tips & Best Practices

1. **Start with broad gates**: Create a parent gate capturing the main population
2. **Then refine**: Select the parent and create child gates for subpopulations
3. **Use appropriate tools**:
   - Rectangles for simple regions
   - Polygons for irregular shapes
   - Ellipses for circular populations
4. **Check statistics**: Stats panel shows percentages and counts
5. **Export when done**: Use Export CSV to save gated events

---

## Example Workflow

```
1. Load file (auto-loads test_sample.fcs)
2. Create rectangle R1 to select main population
3. In Parent Gate selector, click "R1"
4. Create polygon P1 for CD4+ cells (child of R1)
5. Click "Summary" to see distributions
6. Try "Auto Cluster" to find additional populations
7. Export CSV when done
```

Enjoy your enhanced FlowCyt experience!
