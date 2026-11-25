// calder-uia: records UI Automation events of one process, or drives its UI from a script.
//
//   calder-uia record --pid <pid>
//       Prints one JSON object per line: {"type":"ready"} once subscribed, then
//       {"type":"event","kind":...,"qpc":...,"element":{...},"value":...} for invoke, value,
//       selection and toggle events. "qpc" is Stopwatch.GetTimestamp(), i.e. the
//       QueryPerformanceCounter clock the capture DLL also uses.
//
//   calder-uia drive --pid <pid> --script <steps.json>
//       Steps: {"action":"invoke"|"set"|"select"|"toggle"|"wait"|"close","id":..,"value":..,"ms":..}
using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Windows.Automation;

namespace Calder.Uia;

internal static class Program
{
    private static readonly object OutputLock = new();

    private static int Main(string[] args)
    {
        try
        {
            if (args.Length >= 3 && args[0] == "record" && args[1] == "--pid")
            {
                return Record(int.Parse(args[2]));
            }
            if (args.Length >= 5 && args[0] == "drive" && args[1] == "--pid" && args[3] == "--script")
            {
                return Drive(int.Parse(args[2]), args[4]);
            }
            Console.Error.WriteLine("usage: calder-uia record --pid <pid> | drive --pid <pid> --script <file>");
            return 2;
        }
        catch (Exception ex) when (ex is ElementNotAvailableException or InvalidOperationException or ArgumentException
                                       or IOException or JsonException or FormatException or TimeoutException)
        {
            Emit(new JsonObject { ["type"] = "error", ["message"] = ex.Message });
            Console.Error.WriteLine(ex.Message);
            return 1;
        }
    }

    // ---- output -------------------------------------------------------------------------------

    private static void Emit(JsonObject obj)
    {
        lock (OutputLock)
        {
            Console.Out.WriteLine(obj.ToJsonString());
            Console.Out.Flush();
        }
    }

    private static string Label(AutomationElement element)
    {
        try
        {
            var labeledBy = element.Current.LabeledBy;
            if (labeledBy != null && !string.IsNullOrWhiteSpace(labeledBy.Current.Name))
            {
                return labeledBy.Current.Name;
            }
            // Inputs without LabeledBy: fall back to the nearest preceding text sibling, the
            // usual label placement. Buttons and other controls carry their own names.
            var type = element.Current.ControlType;
            if (type != ControlType.Edit && type != ControlType.ComboBox && type != ControlType.Spinner &&
                type != ControlType.Slider && type != ControlType.CheckBox && type != ControlType.RadioButton)
            {
                return "";
            }
            var walker = TreeWalker.ControlViewWalker;
            var sibling = walker.GetPreviousSibling(element);
            for (var i = 0; sibling != null && i < 3; i++, sibling = walker.GetPreviousSibling(sibling))
            {
                if (sibling.Current.ControlType == ControlType.Text && !string.IsNullOrWhiteSpace(sibling.Current.Name))
                {
                    return sibling.Current.Name;
                }
            }
        }
        catch (ElementNotAvailableException)
        {
        }
        return "";
    }

    private static JsonObject Describe(AutomationElement element, string window)
    {
        var info = new JsonObject { ["window"] = window };
        try
        {
            var current = element.Current;
            info["name"] = current.Name;
            info["automation_id"] = current.AutomationId;
            info["control_type"] = current.ControlType.ProgrammaticName.Replace("ControlType.", "");
            info["label"] = Label(element);
        }
        catch (ElementNotAvailableException)
        {
            info["name"] = "";
        }
        return info;
    }

    private static void EmitEvent(string kind, AutomationElement element, string window, string? value)
    {
        var obj = new JsonObject
        {
            ["type"] = "event",
            ["kind"] = kind,
            ["qpc"] = Stopwatch.GetTimestamp(),
            ["element"] = Describe(element, window),
        };
        if (value != null)
        {
            obj["value"] = value;
        }
        Emit(obj);
    }

    // ---- recording ----------------------------------------------------------------------------

    private static List<AutomationElement> Windows(int pid)
    {
        var condition = new PropertyCondition(AutomationElement.ProcessIdProperty, pid);
        return AutomationElement.RootElement.FindAll(TreeScope.Children, condition).Cast<AutomationElement>().ToList();
    }

    private static List<AutomationElement> WaitForWindows(int pid, TimeSpan timeout)
    {
        var sw = Stopwatch.StartNew();
        while (sw.Elapsed < timeout)
        {
            var windows = Windows(pid);
            if (windows.Count > 0)
            {
                return windows;
            }
            Thread.Sleep(100);
        }
        throw new TimeoutException($"no window of process {pid} appeared");
    }

    private static AutomationElement? SelectionOwner(AutomationElement item)
    {
        var walker = TreeWalker.ControlViewWalker;
        for (var parent = walker.GetParent(item); parent != null; parent = walker.GetParent(parent))
        {
            var type = parent.Current.ControlType;
            if (type == ControlType.ComboBox || type == ControlType.List || type == ControlType.Tab)
            {
                return parent;
            }
        }
        return null;
    }

    // The event source is normally the selected item; some containers raise it on themselves,
    // in which case the current selection is read from the container.
    private static string SelectedName(AutomationElement source)
    {
        var type = source.Current.ControlType;
        if ((type == ControlType.ComboBox || type == ControlType.List || type == ControlType.Tab) &&
            source.TryGetCurrentPattern(SelectionPattern.Pattern, out var pattern))
        {
            var selected = ((SelectionPattern)pattern).Current.GetSelection();
            if (selected.Length > 0)
            {
                return selected[0].Current.Name;
            }
        }
        return source.Current.Name;
    }

    private static void Subscribe(AutomationElement window, HashSet<int> seen)
    {
        var hash = window.GetRuntimeId().Aggregate(17, (acc, v) => acc * 31 + v);
        if (!seen.Add(hash))
        {
            return;
        }
        var title = window.Current.Name;
        Automation.AddAutomationEventHandler(InvokePattern.InvokedEvent, window, TreeScope.Subtree,
            (sender, _) => EmitEvent("invoke", (AutomationElement)sender, title, null));
        Automation.AddAutomationEventHandler(SelectionItemPattern.ElementSelectedEvent, window, TreeScope.Subtree,
            (sender, _) =>
            {
                var item = (AutomationElement)sender;
                var owner = SelectionOwner(item) ?? item;
                EmitEvent("selection", owner, title, SelectedName(item));
            });
        Automation.AddAutomationPropertyChangedEventHandler(window, TreeScope.Subtree,
            (sender, e) =>
            {
                var element = (AutomationElement)sender;
                if (e.Property == TogglePattern.ToggleStateProperty)
                {
                    EmitEvent("toggle", element, title, e.NewValue?.ToString());
                }
                else if (element.Current.ControlType != ControlType.ComboBox)
                {
                    EmitEvent("value", element, title, e.NewValue?.ToString() ?? "");
                }
            },
            ValuePattern.ValueProperty, TogglePattern.ToggleStateProperty);
    }

    private static int Record(int pid)
    {
        var process = Process.GetProcessById(pid);
        var seen = new HashSet<int>();
        foreach (var window in WaitForWindows(pid, TimeSpan.FromSeconds(60)))
        {
            Subscribe(window, seen);
        }
        Automation.AddAutomationEventHandler(WindowPattern.WindowOpenedEvent, AutomationElement.RootElement,
            TreeScope.Children, (sender, _) =>
            {
                var window = (AutomationElement)sender;
                try
                {
                    if (window.Current.ProcessId == pid)
                    {
                        Subscribe(window, seen);
                        EmitEvent("window", window, window.Current.Name, null);
                    }
                }
                catch (ElementNotAvailableException)
                {
                }
            });
        Emit(new JsonObject { ["type"] = "ready", ["windows"] = seen.Count, ["frequency"] = Stopwatch.Frequency });
        process.WaitForExit();
        Automation.RemoveAllEventHandlers();
        return 0;
    }

    // ---- driving ------------------------------------------------------------------------------

    private static AutomationElement Find(int pid, string id, TimeSpan timeout)
    {
        var sw = Stopwatch.StartNew();
        var condition = new PropertyCondition(AutomationElement.AutomationIdProperty, id);
        while (sw.Elapsed < timeout)
        {
            foreach (var window in Windows(pid))
            {
                var found = window.FindFirst(TreeScope.Descendants, condition);
                if (found != null)
                {
                    return found;
                }
            }
            Thread.Sleep(100);
        }
        throw new TimeoutException($"element '{id}' not found");
    }

    private static void Select(AutomationElement combo, string itemName)
    {
        if (combo.TryGetCurrentPattern(ExpandCollapsePattern.Pattern, out var expand))
        {
            ((ExpandCollapsePattern)expand).Expand();
            Thread.Sleep(150);
        }
        var item = combo.FindFirst(TreeScope.Descendants, new PropertyCondition(AutomationElement.NameProperty, itemName))
                   ?? throw new InvalidOperationException($"no item '{itemName}'");
        ((SelectionItemPattern)item.GetCurrentPattern(SelectionItemPattern.Pattern)).Select();
        if (expand != null)
        {
            ((ExpandCollapsePattern)expand).Collapse();
        }
    }

    private static int Drive(int pid, string scriptPath)
    {
        var steps = JsonNode.Parse(File.ReadAllText(scriptPath))?.AsArray()
                    ?? throw new JsonException("script must be a JSON array");
        WaitForWindows(pid, TimeSpan.FromSeconds(60));
        var timeout = TimeSpan.FromSeconds(15);
        foreach (var node in steps)
        {
            var step = node!.AsObject();
            var action = step["action"]?.GetValue<string>() ?? throw new JsonException("step without action");
            var id = step["id"]?.GetValue<string>();
            var value = step["value"]?.ToString();
            switch (action)
            {
                case "invoke":
                    ((InvokePattern)Find(pid, id!, timeout).GetCurrentPattern(InvokePattern.Pattern)).Invoke();
                    break;
                case "set":
                    ((ValuePattern)Find(pid, id!, timeout).GetCurrentPattern(ValuePattern.Pattern)).SetValue(value ?? "");
                    break;
                case "select":
                    Select(Find(pid, id!, timeout), value ?? "");
                    break;
                case "toggle":
                    ((TogglePattern)Find(pid, id!, timeout).GetCurrentPattern(TogglePattern.Pattern)).Toggle();
                    break;
                case "wait":
                    Thread.Sleep(step["ms"]?.GetValue<int>() ?? 500);
                    break;
                case "close":
                    foreach (var window in Windows(pid))
                    {
                        if (window.TryGetCurrentPattern(WindowPattern.Pattern, out var pattern))
                        {
                            ((WindowPattern)pattern).Close();
                        }
                    }
                    break;
                default:
                    throw new JsonException($"unknown action '{action}'");
            }
            Emit(new JsonObject { ["type"] = "step", ["action"] = action, ["id"] = id });
            Thread.Sleep(step["delay_ms"]?.GetValue<int>() ?? 250);
        }
        return 0;
    }
}
