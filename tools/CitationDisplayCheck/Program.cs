using System.Reflection;
using System.Text.Json.Nodes;
using DesktopCompanion.Desktop.ApplicationServices;
using DesktopCompanion.Desktop.Presentation;
using DesktopCompanion.Desktop.Protocol;

var viewModel = new MainWindowViewModel(new OfflineClient(), new Uri("ws://127.0.0.1:1"));
var showResult = typeof(MainWindowViewModel).GetMethod("ShowResult", BindingFlags.Instance | BindingFlags.NonPublic)!;
void Check(JsonObject citation, params string[] expected)
{
    showResult.Invoke(viewModel, [new JsonObject { ["status"] = "ok", ["citations"] = new JsonArray(citation) }]);
    foreach (var value in expected)
        if (!viewModel.CitationText.Contains(value)) throw new Exception("Missing display value: " + value);
    if (viewModel.SelectedResultTab != 0) throw new Exception("Citations tab was not selected.");
}
Check(new JsonObject {
    ["title"] = "旧文档", ["version"] = "v1", ["section"] = "回访",
    ["line_start"] = 5, ["line_end"] = 10, ["quote"] = "每周回访",
    ["source"] = "模拟演示", ["collection_id"] = "legacy_sales_demo",
}, "第 5–10 行", "每周回访", "legacy_sales_demo");
Check(new JsonObject {
    ["title"] = "文字与表格", ["version"] = "test-v1", ["section"] = "表格",
    ["page"] = 2, ["bbox"] = new JsonArray(50, 195, 490, 231),
    ["bbox_units"] = "pdf_points", ["table_id"] = "P2-T1", ["row_number"] = 3,
    ["quote"] = "检测计数 | 17", ["source"] = "检测资料",
    ["collection_id"] = "import_detection", ["original_path"] = "test-original.pdf",
}, "第 2 页", "P2-T1", "第 3 行", "检测计数 | 17", "PDF 页坐标", "test-original.pdf");
if (viewModel.CitationText.Contains("第 – 行")) throw new Exception("PDF claimed text line numbers.");
Check(new JsonObject {
    ["title"] = "扫描图片", ["version"] = "test-v1", ["section"] = "扫描图片",
    ["page"] = 1, ["method"] = "windows_ocr", ["quote"] = "检查识别文字和引用位置",
    ["bbox"] = new JsonArray(70, 369, 532, 411), ["bbox_units"] = "oriented_image_pixels",
    ["correction_note"] = "对照原件更正。", ["source"] = "检测资料",
    ["collection_id"] = "import_detection", ["original_path"] = "test-original.png",
}, "第 1 页", "OCR 已复核", "更正记录", "图片像素", "test-original.png");
showResult.Invoke(viewModel, [new JsonObject { ["status"] = "insufficient_evidence" }]);
if (viewModel.CitationText.Length != 0 || viewModel.SelectedResultTab != 3)
    throw new Exception("Stale citations were shown after insufficient evidence.");
Console.WriteLine("Citation display checks passed: legacy lines, PDF table, reviewed OCR, empty result.");

sealed class OfflineClient : IAgentClientService
{
    public bool IsConnected => false;
    public event Action<AgentMessage>? MessageReceived { add { } remove { } }
    public event Action<Exception>? ReceiveFailed { add { } remove { } }
    public void StartNewSession() { }
    public Task ConnectAsync(Uri endpoint, CancellationToken cancellationToken = default) => throw new Exception("No network allowed.");
    public Task DisconnectAsync(CancellationToken cancellationToken = default) => Task.CompletedTask;
    public Task SendChatAsync(string message, CancellationToken cancellationToken = default) => throw new Exception("No network allowed.");
    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}
