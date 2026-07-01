import argparse
import json
import os
import sys

from .config import VERSION, APP_NAME
from .extractor import extract_pdf
from .cloud_sync import CloudSync


def main():
    parser = argparse.ArgumentParser(
        description=f"{APP_NAME} v{VERSION} - Extract data from Australian rental condition report PDFs"
    )
    parser.add_argument("pdf", nargs="?", help="Path to the PDF file to extract")
    parser.add_argument("-j", "--jurisdiction", default="NSW",
                        choices=["NSW", "VIC", "QLD", "SA", "WA", "TAS", "ACT", "NT"],
                        help="Australian jurisdiction (default: NSW)")
    parser.add_argument("-t", "--type", dest="report_type", default="auto",
                        choices=["auto", "move_in", "move_out", "combined"],
                        help="Report type (default: auto)")
    parser.add_argument("-o", "--output", help="Output directory (default: same as PDF)")
    parser.add_argument("--endpoint", help="Cloud sync endpoint URL")
    parser.add_argument("--api-key", help="API key for cloud endpoint")
    parser.add_argument("--no-images", action="store_true", help="Skip image extraction")
    parser.add_argument("--batch", nargs="+", help="Batch process multiple PDF files")
    parser.add_argument("--gui", action="store_true", help="Launch the GUI")
    parser.add_argument("-v", "--version", action="version", version=f"{APP_NAME} v{VERSION}")

    args = parser.parse_args()

    if args.gui:
        from .gui import run_gui
        run_gui()
        return

    pdf_files = []
    if args.batch:
        pdf_files = args.batch
    elif args.pdf:
        pdf_files = [args.pdf]
    else:
        parser.print_help()
        sys.exit(1)

    for pdf_path in pdf_files:
        if not os.path.isfile(pdf_path):
            print(f"ERROR: File not found: {pdf_path}")
            continue

        output_dir = args.output or os.path.dirname(os.path.abspath(pdf_path))
        os.makedirs(output_dir, exist_ok=True)

        print(f"Processing: {pdf_path}")
        print(f"  Jurisdiction: {args.jurisdiction}")
        print(f"  Report type: {args.report_type}")

        try:
            result = extract_pdf(
                pdf_path,
                jurisdiction=args.jurisdiction,
                report_type=args.report_type,
                output_dir=output_dir,
                save_images=not args.no_images,
            )

            base_name = os.path.splitext(os.path.basename(pdf_path))[0]
            json_path = os.path.join(output_dir, f"{base_name}_extracted.json")

            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)

            area_count = len(result.get("areas", []))
            component_count = sum(len(a.get("components", [])) for a in result.get("areas", []))
            image_count = len(result.get("images", []))

            print(f"  Jurisdiction: {result.get('jurisdiction', '')}")
            print(f"  Document type: {result.get('document_type', '')}")
            print(f"  Areas: {area_count}")
            print(f"  Components: {component_count}")
            print(f"  Images: {image_count}")
            print(f"  JSON saved: {json_path}")

            if args.endpoint:
                print(f"  Syncing to: {args.endpoint}")
                sync = CloudSync(endpoint_url=args.endpoint, api_key=args.api_key)
                sync_result = sync.sync(result)
                if sync_result["success"]:
                    print(f"  Cloud sync OK (status {sync_result['status_code']})")
                else:
                    print(f"  Cloud sync FAILED: {sync_result['error']}")

            print(f"  Done!")

        except Exception as e:
            print(f"  ERROR: {e}")

    print(f"\nAll files processed.")


if __name__ == "__main__":
    main()
